"""Raise a coordinator session's native spending limit before each turn.

A session with helpers (`daimon.core.subagents`) is created with a Managed Agents
budget of one turn's worth (`session_budget`); Managed Agents accepts a budget
only at creation and lets an existing one be raised. Before each later turn's
message is sent, the limit becomes what the session has spent so far plus one
turn's worth (`turn_limit_cents`), so every request, the lead and all its helpers
together, is capped at `DAIMON_SUBAGENTS__TURN_BUDGET_USD` at list price.

A session that stopped at its limit is retired by the turn that hit it
(`turn.run`), so a raise here never resumes paused work. A raise that fails
refuses the turn: it never runs uncapped.
"""

from __future__ import annotations

import structlog
from anthropic import AsyncAnthropic
from daimon.core.errors import TurnError
from daimon.core.mux_backend import managed_agents, resource_ref
from daimon.core.mux_compat import legacy_call
from daimon.core.subagents import turn_limit_cents
from mux.contracts.ids import Scope
from mux.drivers.anthropic import session_threads

log = structlog.get_logger(__name__)

__all__ = ["raise_turn_budget"]


async def raise_turn_budget(anthropic: AsyncAnthropic, session_id: str, *, scope: Scope) -> None:
    """Set the session's limit to its spend so far plus one turn's worth.

    Raises `TurnError` (upstream) when the limit cannot be read or set, or the
    session has none: the turn must not run without one.
    """
    backend = managed_agents(anthropic, scope=scope, resources=frozenset({("session", session_id)}))
    ref = resource_ref(backend, "session", session_id, scope=scope)
    port = session_threads(backend)
    try:
        spent = await legacy_call(port.spent_cents(scope, ref))
        if spent is None:
            raise TurnError(kind="upstream", message="the helpers' session has no spending limit")
        limit = turn_limit_cents(spent)
        await legacy_call(port.set_budget(scope, ref, max_cents=limit))
    except TurnError:
        raise
    except Exception as error:  # a named boundary: any failure refuses the turn
        log.warning(
            "subagents.budget_raise_failed", session_id=session_id, error=type(error).__name__
        )
        raise TurnError(
            kind="upstream", message="couldn't set this request's spending limit", cause=error
        ) from error
    log.info("subagents.turn_budget", session_id=session_id, spent_cents=spent, limit_cents=limit)
