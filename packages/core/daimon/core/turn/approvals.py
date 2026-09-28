"""Pure decision logic for `AutoApprove` tool confirmations.

This module is pure — no `anthropic` client, no `httpx`, no DB, no clock —
so the dedup rule (which `tool_use_id`s are still fresh) is unit-testable
without a fake stream, which is exactly where a reconnect-dedup bug would
otherwise hide. The SEND itself (`anthropic.beta.sessions.events.send(...)`)
stays in `driver.py` because it is I/O; this module only decides WHAT to
send.

`build_confirmation_events` hardcodes `result="allow"` because it serves
`AutoApprove` only. The per-call posture, `PolicyApproval`, has its own pair:
`tool_calls_for` turns the blocked ids into `ToolCall`s from the folded
state, and `build_decision_events` sends each call's own allow/deny.

The two decider builders are the policy shells over
`daimon.core.tool_safety.decide_tool_call`. `unattended_decider` never waits
on anyone; `interactive_decider` hands `ask` verdicts to the adapter's
`ConfirmationHook`. Neither does I/O of its own — the hook is the only thing
that talks to a platform, and a hook that raises counts as a refusal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime

import structlog
from anthropic.types.beta.sessions import BetaManagedAgentsUserToolConfirmationEventParams
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import StopReason
from daimon.core.confirmation import (
    ConfirmationHook,
    no_confirmation_surface,
    prompt_for_tool_call,
)
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy, ToolVerdict, decide_tool_call
from daimon.core.turn.posture import (
    AutoApprove,
    PolicyApproval,
    RequireApproval,
    ToolCallDecider,
    ToolConfirmation,
    ToolConfirmationResult,
)
from daimon.core.turn.state import ToolUseBlock, TurnState

log = structlog.get_logger(__name__)

#: Stand-in for a blocked id whose `tool_use` event never reached the folded
#: state. Unknown server and tool: `decide_tool_call` classifies it a write,
#: so it is asked about or refused, never waved through.
_UNKNOWN_TOOL = "unknown"


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


def tool_calls_for(state: TurnState, ids: Sequence[str]) -> list[ToolCall]:
    """One `ToolCall` per blocked id, from the tool-use blocks in `state`."""
    blocks = {b.id: b for b in state.content if isinstance(b, ToolUseBlock)}
    calls: list[ToolCall] = []
    for tool_use_id in ids:
        block = blocks.get(tool_use_id)
        if block is None:
            calls.append(
                ToolCall(
                    tool_use_id=tool_use_id, server_name=_UNKNOWN_TOOL, tool_name=_UNKNOWN_TOOL
                )
            )
            continue
        calls.append(
            ToolCall(
                tool_use_id=tool_use_id,
                server_name=block.mcp_server_name,
                tool_name=block.name,
                input=dict(block.input),
            )
        )
    return calls


def build_decision_events(
    decisions: Iterable[tuple[str, ToolConfirmationResult]],
) -> list[BetaManagedAgentsUserToolConfirmationEventParams]:
    """One `user.tool_confirmation` per `(tool_use_id, result)`, in order."""
    events: list[BetaManagedAgentsUserToolConfirmationEventParams] = []
    for tool_use_id, result in decisions:
        if result.allow:
            events.append(
                BetaManagedAgentsUserToolConfirmationEventParams(
                    type="user.tool_confirmation", result="allow", tool_use_id=tool_use_id
                )
            )
            continue
        event = BetaManagedAgentsUserToolConfirmationEventParams(
            type="user.tool_confirmation", result="deny", tool_use_id=tool_use_id
        )
        if result.deny_message:
            event["deny_message"] = result.deny_message
        events.append(event)
    return events


def refusal_message(call: ToolCall, verdict: ToolVerdict) -> str:
    """What the model is told when `call` is refused without a person."""
    if verdict.reason == "denied_by_operator":
        return f"Refused: the operator does not allow {call.key}. Do not retry it."
    return (
        f"Refused: {call.key} changes data outside this session and nobody is here to "
        "approve it, so it did not run. Do not retry it in this run; report what you "
        "would have written instead."
    )


def _answer_message(call: ToolCall, answer: str) -> str:
    if answer == "expired":
        return f"Nobody approved {call.key} in time, so it did not run. Do not retry it."
    return (
        f"The user denied {call.key}; it did not run. Do not retry it. Ask them what "
        "they want instead."
    )


def unattended_decider(policy: ToolSafetyPolicy) -> ToolCallDecider:
    """Decider for runs nobody is watching: reads run, writes are refused
    unless the operator allowed them there."""

    async def _decide(call: ToolCall) -> ToolConfirmationResult:
        verdict = decide_tool_call(policy, call, attended=False)
        log.info(
            "tool_safety.decided",
            tool=call.key,
            outcome=verdict.outcome,
            reason=verdict.reason,
            attended=False,
        )
        if verdict.outcome == "allow":
            return ToolConfirmationResult(allow=True)
        return ToolConfirmationResult(allow=False, deny_message=refusal_message(call, verdict))

    return _decide


def interactive_decider(
    policy: ToolSafetyPolicy,
    *,
    requester_platform_user_id: str,
    confirm: ConfirmationHook = no_confirmation_surface,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ToolCallDecider:
    """Decider for chat: reads run, writes wait for `confirm`.

    `confirm` is the adapter's card hook; the default refuses, which is what a
    surface without cards gets. A hook that raises is logged and counts as a
    refusal — a write never runs because the card failed to post.
    """

    async def _decide(call: ToolCall) -> ToolConfirmationResult:
        verdict = decide_tool_call(policy, call, attended=True)
        log.info(
            "tool_safety.decided",
            tool=call.key,
            outcome=verdict.outcome,
            reason=verdict.reason,
            attended=True,
        )
        if verdict.outcome == "allow":
            return ToolConfirmationResult(allow=True)
        if verdict.outcome == "deny":
            return ToolConfirmationResult(allow=False, deny_message=refusal_message(call, verdict))
        prompt = prompt_for_tool_call(
            call, requester_platform_user_id=requester_platform_user_id, now=now()
        )
        try:
            answer = await confirm(prompt)
        except Exception as err:
            # Named boundary: the hook is adapter code talking to a chat API;
            # whatever it raises, the write must not run.
            log.warning("tool_safety.confirm_failed", tool=call.key, error=str(err))
            answer = "denied"
        log.info("tool_safety.answered", tool=call.key, answer=answer)
        if answer == "approved":
            return ToolConfirmationResult(allow=True)
        return ToolConfirmationResult(allow=False, deny_message=_answer_message(call, answer))

    return _decide


def headless_tool_confirmation(policy: ToolSafetyPolicy) -> ToolConfirmation:
    """Posture for a run nobody is watching (routines, smoke, transfers).

    Disabled keeps `AutoApprove`, byte-identical to before the policy existed.
    """
    if not policy.enabled:
        return AutoApprove()
    return PolicyApproval(decide=unattended_decider(policy))


def chat_tool_confirmation(
    policy: ToolSafetyPolicy,
    *,
    requester_platform_user_id: str,
    confirm: ConfirmationHook | None,
    attended: bool = True,
) -> ToolConfirmation:
    """Posture for a chat turn.

    Disabled keeps `RequireApproval`, as before. `attended=False` is a chat
    turn nobody asked for just now (a wake continuing earlier work): it gets
    the unattended rules even though it renders into a thread.
    """
    if not policy.enabled:
        return RequireApproval()
    if not attended:
        return PolicyApproval(decide=unattended_decider(policy))
    return PolicyApproval(
        decide=interactive_decider(
            policy,
            requester_platform_user_id=requester_platform_user_id,
            confirm=confirm if confirm is not None else no_confirmation_surface,
        )
    )
