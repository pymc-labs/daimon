"""May this task be handed to that agent, in this thread, right now.

One pure decision function plus the one typed refusal a store has to raise.
Every input is a fact the caller already read — whether the destination is
reachable, what kind of binding the thread already carries, who answers now —
so the rule itself has no I/O and is exhaustively testable.

The refusals are ordered, and the order is the point:

1. `setup_thread` — a setup conversation must always answer as the built-in
   Daimon, so nothing can be handed over inside one. Nothing else matters
   once the location is a setup thread.
2. The access policy, as `authorize(HAND_OFF)` decided it: `writers_none`,
   `invoker_not_allowed`, `runs_elsewhere` and `own_agents_only`. Checked
   before reachability: an agent with a rule is reachable (it answers in its
   own channels), and that is exactly what must not carry it here.
3. `unreachable` — an agent nobody can reach through the channel/workspace
   cascade cannot be handed a task, because the person could never talk to
   it afterwards.
4. `same_agent` — the destination already answers here; there is nothing to
   hand over.
5. `admin_required` / `sealed` — the destination is not scoped to this
   channel, and the caller may not bring it in (`authorize` says who may). A
   handed-off thread runs as the destination, with its repo, keys, connectors
   and memory, so bringing another project's agent into a channel is an
   admin's call.

Pure module — no I/O, no clock, no randomness.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from daimon.core.errors import DaimonError
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    # Annotation only: authz imports the stores, which import this module.
    from daimon.core.authz import Decision

__all__ = [
    "HandoffAllowed",
    "HandoffDecision",
    "HandoffRefusalReason",
    "HandoffRefused",
    "HandoffRefusedInSetupThread",
    "decide_handoff",
]

HandoffRefusalReason = Literal[
    "unreachable",
    "setup_thread",
    "same_agent",
    "runs_elsewhere",
    "admin_required",
    "writers_none",
    "invoker_not_allowed",
    "own_agents_only",
    "not_a_reader",
]

# `authorize(HAND_OFF)` denials that refuse before reachability is looked at.
_POLICY_REFUSALS: dict[str, HandoffRefusalReason] = {
    "writers_none": "writers_none",
    "invoker_not_allowed": "invoker_not_allowed",
    "runs_elsewhere": "runs_elsewhere",
    "agent_unresolved": "unreachable",
    "own_agents_only": "own_agents_only",
}


class HandoffRefusedInSetupThread(DaimonError):
    """A handoff binding was attempted over a setup conversation's binding.

    Raised by the store that writes the binding, not only decided here: the
    location row is re-read under `FOR UPDATE` at write time, so a setup
    conversation opened between the decision and the write is still refused.
    """


class HandoffAllowed(BaseModel):
    """The handoff may proceed to its writes."""

    model_config = ConfigDict(frozen=True)

    destination_ma_agent_id: str
    destination_name: str


class HandoffRefused(BaseModel):
    """The handoff must not happen, and why."""

    model_config = ConfigDict(frozen=True)

    reason: HandoffRefusalReason
    destination_name: str
    # The `authorize(HAND_OFF)` reason behind a policy refusal, for the audit trail.
    authz_reason: str | None = None


HandoffDecision = HandoffAllowed | HandoffRefused


def decide_handoff(
    *,
    destination_ma_agent_id: str,
    destination_name: str,
    destination_reachable: bool,
    existing_binding_kind: Literal["setup", "handoff", "opened"] | None,
    origin_responder_ma_agent_id: str | None,
    access: Decision,
) -> HandoffDecision:
    """Decide whether this task may move to `destination_ma_agent_id`.

    `destination_ma_agent_id` is always concrete: the caller resolved it from
    the tenant's live agents by exact id, so a recreated namesake is a
    different destination and never silently inherits a handoff.

    `access` is `authorize(HAND_OFF)` for the destination at this thread, from
    a policy read under the tenant's policy lock.

    `existing_binding_kind` is the kind of binding the thread already carries
    (None when it carries none). A `handoff` binding is replaceable — a task
    can move on again — a `setup` one is not. `origin_responder_ma_agent_id`
    is who answers now; None skips the same-agent check (a switch out of a
    session the new responder can't use yet).
    """
    if existing_binding_kind == "setup":
        return HandoffRefused(reason="setup_thread", destination_name=destination_name)
    denied = None if access.allowed else access.reason
    if denied is not None and denied in _POLICY_REFUSALS:
        return HandoffRefused(
            reason=_POLICY_REFUSALS[denied], destination_name=destination_name, authz_reason=denied
        )
    if not destination_reachable:
        return HandoffRefused(reason="unreachable", destination_name=destination_name)
    if destination_ma_agent_id == origin_responder_ma_agent_id:
        return HandoffRefused(reason="same_agent", destination_name=destination_name)
    if denied is not None:
        return HandoffRefused(
            reason="not_a_reader" if denied == "not_a_reader" else "admin_required",
            destination_name=destination_name,
            authz_reason=denied,
        )
    return HandoffAllowed(
        destination_ma_agent_id=destination_ma_agent_id, destination_name=destination_name
    )
