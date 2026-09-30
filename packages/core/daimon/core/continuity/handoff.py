"""May this task be handed to that agent, in this thread, right now.

One pure decision function plus the one typed refusal a store has to raise.
Every input is a fact the caller already read — whether the destination is
reachable, what kind of binding the thread already carries, who answers now —
so the rule itself has no I/O and is exhaustively testable.

The refusals are ordered, and the order is the point:

1. `setup_thread` — a setup conversation must always answer as the built-in
   Daimon, so nothing can be handed over inside one. Nothing else matters
   once the location is a setup thread.
2. `pinned_elsewhere` — an operator pinned the destination to other
   channels, so it must not be brought into this one. Checked before
   reachability: a pinned agent is reachable (it answers in its own
   channels), and that is exactly what must not carry it here.
3. `unreachable` — an agent nobody can reach through the channel/workspace
   cascade cannot be handed a task, because the person could never talk to
   it afterwards.
4. `same_agent` — the destination already answers here; there is nothing to
   hand over.
5. `admin_required` — the destination is not the agent this channel answers
   with, and the caller is not an admin. Every agent reachable anywhere in the
   workspace is reachable here, and a handed-off thread runs as the
   destination, with its repo, keys, connectors and memory. Bringing another
   project's agent into a channel is therefore an admin's call; a member may
   still hand a thread back to the channel's own agent.

Pure module — no I/O, no clock, no randomness.
"""

from __future__ import annotations

from typing import Literal

from daimon.core.errors import DaimonError
from pydantic import BaseModel, ConfigDict

__all__ = [
    "HandoffAllowed",
    "HandoffDecision",
    "HandoffRefusalReason",
    "HandoffRefused",
    "HandoffRefusedInSetupThread",
    "decide_handoff",
]

HandoffRefusalReason = Literal[
    "unreachable", "setup_thread", "same_agent", "pinned_elsewhere", "admin_required"
]


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


HandoffDecision = HandoffAllowed | HandoffRefused


def decide_handoff(
    *,
    destination_ma_agent_id: str,
    destination_name: str,
    destination_reachable: bool,
    existing_binding_kind: Literal["setup", "handoff"] | None,
    origin_responder_ma_agent_id: str,
    destination_pinned_elsewhere: bool = False,
    destination_answers_channel: bool = True,
    caller_is_admin: bool = True,
) -> HandoffDecision:
    """Decide whether this task may move to `destination_ma_agent_id`.

    `destination_ma_agent_id` is always concrete: the caller resolved it from
    the tenant's live agents by exact id, so a recreated namesake is a
    different destination and never silently inherits a handoff.

    `destination_pinned_elsewhere` is True when the tenant's access policy
    pins the destination to channels that don't include this thread's.

    `destination_answers_channel` is True when the channel/workspace cascade
    already sends this thread's parent channel to the destination; unless it
    is, only a `caller_is_admin` caller may hand the thread over.

    `existing_binding_kind` is the kind of binding the thread already carries
    (None when it carries none). A `handoff` binding is replaceable — a task
    can move on again — a `setup` one is not.
    """
    if existing_binding_kind == "setup":
        return HandoffRefused(reason="setup_thread", destination_name=destination_name)
    if destination_pinned_elsewhere:
        return HandoffRefused(reason="pinned_elsewhere", destination_name=destination_name)
    if not destination_reachable:
        return HandoffRefused(reason="unreachable", destination_name=destination_name)
    if destination_ma_agent_id == origin_responder_ma_agent_id:
        return HandoffRefused(reason="same_agent", destination_name=destination_name)
    if not destination_answers_channel and not caller_is_admin:
        return HandoffRefused(reason="admin_required", destination_name=destination_name)
    return HandoffAllowed(
        destination_ma_agent_id=destination_ma_agent_id, destination_name=destination_name
    )
