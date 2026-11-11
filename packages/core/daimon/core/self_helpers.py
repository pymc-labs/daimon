"""Self-helpers: an agent's `[self]` roster on a provider without native multiagent. Pure.

Managed Agents runs `multiagent: {type: coordinator, agents: [{type: self}]}` in the
agent's own session. Every other provider gets the same roster as host helpers
(`daimon.core.helpers`): the agent asks `ask_helper(helper="self")`, and each ask runs a
fresh copy of it under its own key (`self_helper_thread_key`), so a fan-out runs in
parallel. A switch records the roster as translated, never as a loss. Copies share no
sandbox: files move by link, as for any helper.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import cast

from mux.contracts.extensions import ExtensionConfig
from mux.contracts.profile import SUPPORTED, Capability
from mux.profiles import get_profile

#: The roster name the asking agent uses for a copy of itself.
SELF = "self"
#: The capability a self roster needs natively, and what the host translates.
MULTIAGENT: Capability = "multiagent"
#: Spend of one asking turn's self-helpers together, at list price: the native
#: coordinator's per-turn budget default, so both paths stop at the same amount.
TURN_BUDGET_USD = Decimal("10")
_NAMESPACE = "anthropic.multiagent"


def is_self_roster(agents: Sequence[object], own_id: str) -> bool:
    """Whether a coordinator roster names only the agent itself.

    Entries come as written (`{"type": "self"}`, an id string) or resolved
    (`{"type": "agent", "id": ...}`, or an SDK reference with `id`).
    """
    if len(agents) != 1:
        return False
    [entry] = agents
    if isinstance(entry, str):
        return entry == own_id
    if isinstance(entry, Mapping):
        fields = cast(Mapping[str, object], entry)
        return fields.get("type") == SELF or (
            fields.get("type") in (None, "agent") and fields.get("id") == own_id
        )
    return getattr(entry, "type", None) == SELF or getattr(entry, "id", None) == own_id


def self_roster_extension(extension: ExtensionConfig, own_id: str | None) -> bool:
    """Whether `extension` is a version-1 coordinator whose roster is only the agent."""
    if extension.namespace != _NAMESPACE or extension.version != 1 or own_id is None:
        return False
    value = dict(extension.value)
    agents = value.get("agents")
    return (
        value.get("type") == "coordinator"
        and isinstance(agents, list | tuple)
        and is_self_roster(agents, own_id)
    )


def translates_self_roster(profile_id: str) -> bool:
    """Whether a profile runs a self roster as host self-helpers: it lacks native multiagent."""
    return get_profile(profile_id).support_for(MULTIAGENT) not in SUPPORTED


def self_helper_thread_key(thread_id: str, lane: int) -> str:
    """The key of the `lane`th self-helper's conversation in a thread.

    Each self ask of a turn has its own lane, so two copies never share a session
    or a provider slot and a fan-out runs in parallel.
    """
    return f"{thread_id}~helper~{SELF}~{lane}"
