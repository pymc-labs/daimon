"""Parallel helpers: native Managed Agents subagents, behind one deployment switch.

An Anthropic agent whose spec carries `multiagent: {type: coordinator, agents:
[{type: self}]}` can hand pieces of one chat request to copies of itself. Managed
Agents runs each copy as a child *thread* of the lead's own session: same
sandbox, same vaults, and, because the roster is `self`, the same
`agent_with_overrides` the session was created with, so every tool-safety gate
and personal-server filter `create_session` compiles reaches the helpers too.
The helpers report to the lead and the lead writes the reply; the turn ends
only on `session.status_idle`, which the provider emits when no thread runs.

This module owns the host's guardrails, all of them off unless
`DAIMON_SUBAGENTS__ENABLED` is set:

- **write gate** (`check_spec_multiagent`): every agent create or update
  (`mux_compat.create_agent`/`update_agent`, a catalog publish) refuses a
  roster while the switch is off and any roster but `[self]` always (a
  foreign agent id brings its own tools and MCP servers into a vault it
  shares with the lead);
- **session gate** (`require_session_allowed`): a coordinator session starts
  only for a chat turn with the switch on, never for a routine, a smoke run or
  an agent key, so nothing unattended answers a helper's request;
- **budget** (`session_budget`, `turn_limit_cents`): a native session budget is
  attached at creation and raised before each turn to what the session has
  spent plus `turn_budget_usd` (`turn_limit_cents`); Managed Agents stops new model calls past it;
- **framing** (`coordinator_framing`): the lead is told the helper limit and
  that each helper works in its own git worktree.

Billing the helpers' calls, which never reach the session's own event list,
is `usage_sweep.settle_session`'s job.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import ROUND_CEILING, Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Final, cast

from daimon.core.errors import UserFacingError
from mux.drivers.anthropic import MULTIAGENT_NAMESPACE
from pydantic import JsonValue

if TYPE_CHECKING:
    from daimon.core.config import SubagentsSettings

__all__ = [
    "SubagentsError",
    "check_multiagent_value",
    "check_spec_multiagent",
    "coordinator_framing",
    "coordinator_system",
    "is_coordinator",
    "require_session_allowed",
    "session_budget",
    "subagents_override",
    "subagents_settings",
    "turn_limit_cents",
]

_OVERRIDE: ContextVar[SubagentsSettings | None] = ContextVar("subagents_settings", default=None)

#: The one roster v1 admits: copies of the lead itself.
_SELF_ONLY: Final = [{"type": "self"}]

#: Refusals name who can help; they reach an admin editing an agent, or the
#: person whose turn could not start (as the platform's error card).
_OFF = "Helpers are turned off on this Daimon. Ask an admin to turn them on."
_ROSTER = (
    "Helpers can only be copies of this agent itself: set multiagent to "
    "{type: coordinator, agents: [{type: self}]}."
)
_UNATTENDED = "Helpers only work in a chat. Ask in a conversation with the agent."


class SubagentsError(UserFacingError):
    """A helper configuration or session this deployment refuses."""


@lru_cache(maxsize=1)
def _configured() -> SubagentsSettings:
    from daimon.core.config import load_subagents_settings

    return load_subagents_settings()


def subagents_settings() -> SubagentsSettings:
    """`DAIMON_SUBAGENTS__*`, read once per process; `subagents_override` pins it."""
    override = _OVERRIDE.get()
    return override if override is not None else _configured()


@contextmanager
def subagents_override(settings: SubagentsSettings) -> Iterator[None]:
    """Pin the settings inside the block, whatever the environment says."""
    token = _OVERRIDE.set(settings)
    try:
        yield
    finally:
        _OVERRIDE.reset(token)


def check_multiagent_value(value: object) -> None:
    """Refuse a `multiagent` value an agent write must not send.

    None (clear) always passes. Anything else needs the switch on and the
    `[self]` roster exactly.
    """
    if value is None:
        return
    fields = cast(Mapping[str, object], value) if isinstance(value, Mapping) else None
    if fields is not None and fields.get("clear") is True:
        return
    if not subagents_settings().enabled:
        raise SubagentsError(_OFF)
    if (
        fields is None
        or set(fields) != {"type", "agents"}
        or fields.get("type") != "coordinator"
        or fields.get("agents") != _SELF_ONLY
    ):
        raise SubagentsError(_ROSTER)


def check_spec_multiagent(spec: object) -> None:
    """`check_multiagent_value` for a neutral agent spec or patch about to be written.

    The roster travels as the `anthropic.multiagent` extension: a tuple of
    extensions on a spec, a mapping by namespace on a patch.
    """
    extensions = getattr(spec, "extensions", None)
    if not extensions:
        return
    entries = (
        cast(Mapping[str, object], extensions).values()
        if isinstance(extensions, Mapping)
        else cast(tuple[object, ...], extensions)
    )
    for entry in entries:
        if getattr(entry, "namespace", None) == MULTIAGENT_NAMESPACE:
            check_multiagent_value(getattr(entry, "value", None))


def is_coordinator(multiagent: object) -> bool:
    """Whether an agent (or its session snapshot) can start helpers."""
    return multiagent is not None


def require_session_allowed(multiagent: object, *, chat: bool) -> None:
    """Refuse a session that would start helpers outside a chat turn, or while off.

    The roster itself was checked when the agent was written (the provider's
    session snapshot expands it into full agent records, so it is not re-read here).
    """
    if not is_coordinator(multiagent):
        return
    if not subagents_settings().enabled:
        raise SubagentsError(_OFF)
    if not chat:
        raise SubagentsError(_UNATTENDED)


def _cents(usd: Decimal) -> int:
    return int((usd * 100).to_integral_value(rounding=ROUND_CEILING))


def _budget(cents: int) -> dict[str, JsonValue]:
    return {"type": "limit", "max_list_cost": {"amount": str(max(1, cents)), "currency": "USD"}}


def session_budget() -> dict[str, JsonValue]:
    """The budget a coordinator session is created with: one turn's worth.

    Managed Agents takes a budget only at creation; a later turn raises it.
    """
    return _budget(_cents(subagents_settings().turn_budget_usd))


def turn_limit_cents(spent_cents: int) -> int:
    """The limit before a turn: what the session has spent, plus one turn's worth.

    `spent_cents` is the session's reported `usage.list_cost` (whole cents).
    """
    return max(0, spent_cents) + _cents(subagents_settings().turn_budget_usd)


def coordinator_system(
    system_override: str | None, agent_system: str | None, multiagent: object
) -> str | None:
    """The system prompt a session is sent: the framing added for a coordinator.

    `system_override` is the platform variant (`system_for_platform`), None when
    the agent's own prompt is sent. The framing goes before the tenant context
    fragments, which must stay the prompt's last line. Every place that sends or
    hashes a session's prompt calls this, so the drift check sees what was sent.
    """
    if not is_coordinator(multiagent) or not subagents_settings().enabled:
        # Off, a coordinator's sessions are sent no framing: the prompt hash
        # changes, the drift check replaces a live session at its next turn,
        # and the session gate refuses the replacement until the roster goes.
        return system_override
    from daimon.core.context_prompt import FRAGMENT_MARKER

    base = system_override if system_override is not None else agent_system or ""
    prompt, marker, fragments = base.partition(FRAGMENT_MARKER)
    return prompt.rstrip("\n") + coordinator_framing() + marker + fragments


def coordinator_framing() -> str:
    """Appended to a coordinator session's system prompt."""
    limit = subagents_settings().max_concurrent
    return (
        "\n\n## Helpers\n"
        "You can hand independent pieces of a request to copies of yourself (helpers) "
        f"that work in parallel. Run at most {limit} helpers at once. Give each helper "
        "one well-scoped task with everything it needs, because it cannot see this "
        "conversation. In a git repository, give each helper its own worktree and branch "
        "(git worktree add ../wt-NAME -b BRANCH) and never let two helpers share one; "
        "you review and merge their work. While helpers run, do not narrate; write one "
        "answer once they have all reported."
    )
