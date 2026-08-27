"""Billing and tool-confirmation postures for a turn — tagged unions `run_turn`
requires.

Per CONTEXT.md D-05/D-06/D-07: every call to `run_turn` must declare how the
turn is billed. `Billed` carries an event-only recorder (no `session_id`
passthrough — the recorder binds session/tenant context itself via
`functools.partial`, per RESEARCH "Pattern 2"). `BillingExempt` carries a
closed `ExemptReason` literal: widening the set of exempt reasons is a
deliberate, reviewable act (edit the `Literal`, not a free-form string),
not something a caller can invent inline.

Two members exist today. `"cli-operator-run"` is the CLI operator's own
bypass. `"headless-unrecorded"` (plan 19-09) is a headless turn
(`daimon.core.headless_runner`) invoked without a `usage_record_factory` —
genuinely nothing to meter, not the operator bypass. This is deliberately
NOT modeled as `Billed(record=<no-op recorder>)`: that would record a
metered turn that meters nothing, which is a worse lie than an honest
"exempt, and here is why" reason. `Billed` means "this turn's usage is being
recorded somewhere"; a no-op recorder would satisfy the type while breaking
that meaning for anyone reading a `turn.billing_exempt`-adjacent log line or
auditing billing coverage.

Every call to `run_turn` also declares a `ToolConfirmation` posture — how a
`requires_action` idle (the agent paused, waiting for a tool call to be
approved) is handled. `RequireApproval` is the interactive default: a
`requires_action` idle surfaces as an actionable `TurnError`, since no
approval/resume UX is wired for interactive surfaces yet. `AutoApprove`
answers every blocked `tool_use_id` with a `user.tool_confirmation` `allow`
and keeps consuming on the same stream — the unattended-routine posture.
Both are field-less today and modeled as types rather than a bare
`auto_approve_tools: bool` so the call site is self-documenting and so a
future variant (e.g. a policy callback deciding allow/deny per tool) is an
additive change to the union rather than a signature change at every
existing call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)

ExemptReason = Literal["cli-operator-run", "headless-unrecorded"]


class UsageRecorder(Protocol):
    async def __call__(self, *, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None: ...


@dataclass(frozen=True)
class Billed:
    record: UsageRecorder


@dataclass(frozen=True)
class BillingExempt:
    reason: ExemptReason


BillingPosture = Billed | BillingExempt


@dataclass(frozen=True)
class RequireApproval:
    """Interactive default: a `requires_action` idle ends the turn as an
    actionable `TurnError` — no approval/resume UX is wired yet."""


@dataclass(frozen=True)
class AutoApprove:
    """Unattended-routine posture: a `requires_action` idle is answered with
    a `user.tool_confirmation` `allow` per blocked id, once each, and the
    turn keeps consuming on the same stream."""


ToolConfirmation = RequireApproval | AutoApprove
