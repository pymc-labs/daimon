"""The per-turn wall-clock ceiling (D-08/D-09/D-10): the single home for the
ceiling value, the deadline arithmetic, and the ceiling error constructor.

Pure module -- no I/O, no clock reads of its own. `turn_deadline` and
`remaining_s` both take `now` explicitly so this stays clockless per
`guideline:architecture`; callers own the clock read.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from daimon.core.errors import TurnError

__all__ = [
    "TURN_CEILING_S",
    "turn_deadline",
    "remaining_s",
    "CEILING_MESSAGE",
    "ceiling_error",
]

# Wall-clock backstop on a single turn -- not a latency target. Legitimate
# agentic turns run many minutes (notebooks, model fits), so this is set well
# above the longest real turn and exists only to bound the pathological case:
# an MA session that never leaves `running` (e.g. a tool call whose result
# never comes back), or any other await this ceiling wraps that never
# resolves. This value used to live only in Discord's `bot.py` as
# `_TURN_DEADLINE_S`, wrapping just `run_prepared_turn`; it is now the single
# core-owned value enforced at both `bind_session` and `run_prepared_turn`.
#
# The old Discord comment carried a "ponytail" note proposing an upgrade to a
# liveness deadline (time-since-last-event) instead of a flat wall-clock one.
# That upgrade is now obsolete: the driver's eventless-cycle reconnect loop is
# status-checked (it asks MA whether the session is still `running` before
# reconnecting, rather than trusting silence), so the only thing left for this
# ceiling to bound is a session MA will legitimately keep reporting as
# `running` forever -- legal server behavior, since no server-side wall-clock
# kill exists. That is a backstop value, not a liveness signal, so the flat
# deadline is still the right shape.
TURN_CEILING_S: float = 45.0 * 60.0

# The clamp floor for remaining_s -- see its docstring for why a floor exists.
_MIN_REMAINING_S: float = 0.001


def turn_deadline(*, now: datetime, ceiling_s: float = TURN_CEILING_S) -> datetime:
    """The wall-clock instant a turn started at `now` must finish by."""
    return now + timedelta(seconds=ceiling_s)


def remaining_s(deadline: datetime, *, now: datetime) -> float:
    """Seconds left until `deadline`, clamped to a small positive floor.

    `asyncio.wait_for` requires a positive timeout -- zero or a negative
    value raises `ValueError` before the wrapped coroutine gets a chance to
    run at all. Clamping to `_MIN_REMAINING_S` means an already-exhausted
    deadline still produces a legal timeout, one that fires at the wrapped
    coroutine's first await point rather than crashing the caller outright.
    """
    return max((deadline - now).total_seconds(), _MIN_REMAINING_S)


# D-08: rendered through the adapters' existing generic error path, both of
# which truncate to `str(err)[:100]`. `TurnError.__str__` is `f"{kind}: {msg}"`,
# so this message must leave room for the "ceiling: " prefix -- see
# `ceiling_error` and the length invariant pinned in test_ceiling.py.
CEILING_MESSAGE: str = (
    "this turn stopped responding and was abandoned — start fresh with a new message"
)


def ceiling_error() -> TurnError:
    """Build the one ceiling `TurnError` both enforcement sites raise/return.

    A single constructor so `bind_session` and `run_prepared_turn` cannot
    drift on kind or message.
    """
    return TurnError(kind="ceiling", message=CEILING_MESSAGE)
