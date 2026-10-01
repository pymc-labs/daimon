"""Unit tests for daimon.core.turn.ceiling -- the pure deadline arithmetic and
error constructor D-08/D-09/D-10 build on, plus the D-10 no-recovery pin for
`_is_dead_session`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anthropic
import httpx
from daimon.core.errors import TurnError
from daimon.core.turn.ceiling import (
    TURN_CEILING_S,
    ceiling_error,
    remaining_s,
    turn_deadline,
)
from daimon.core.turn.run import _is_dead_session
from daimon.core.turn.state import TurnState

_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)


def test_turn_deadline_adds_the_default_ceiling() -> None:
    deadline = turn_deadline(now=_NOW)

    assert deadline == _NOW + timedelta(seconds=TURN_CEILING_S)


def test_turn_deadline_honours_an_explicit_ceiling_s() -> None:
    deadline = turn_deadline(now=_NOW, ceiling_s=30.0)

    assert deadline == _NOW + timedelta(seconds=30.0)


def test_remaining_s_returns_positive_time_left_when_deadline_is_in_the_future() -> None:
    deadline = _NOW + timedelta(seconds=10)

    assert remaining_s(deadline, now=_NOW) == 10.0


def test_remaining_s_clamps_to_a_positive_floor_when_deadline_is_now() -> None:
    result = remaining_s(_NOW, now=_NOW)

    assert result > 0, "asyncio.wait_for requires a strictly positive timeout"


def test_remaining_s_clamps_to_a_positive_floor_when_deadline_is_already_past() -> None:
    deadline = _NOW - timedelta(seconds=5)

    result = remaining_s(deadline, now=_NOW)

    assert result > 0, "an already-exhausted deadline must still produce a legal timeout"


def test_ceiling_error_has_kind_ceiling() -> None:
    err = ceiling_error()

    assert err.kind == "ceiling"


def test_ceiling_error_message_fits_the_adapters_100_char_render_budget() -> None:
    # Both adapters render str(err)[:100] -- confirm the full, untruncated
    # rendering (kind prefix included) already fits inside that budget.
    assert len(str(ceiling_error())) <= 100


def test_is_dead_session_never_recovers_a_ceiling_error() -> None:
    """D-10: a ceiling breach must never be misread as a dead-session signal --
    that would re-run a 45-minute turn as a fresh one, doubling wall clock and
    tokens spent (T-19-03-B)."""
    state = TurnState(error=TurnError(kind="ceiling", message="abandoned"))

    assert _is_dead_session(state) is False


def test_is_dead_session_never_recovers_a_ceiling_error_even_with_a_404_shaped_cause() -> None:
    """The kind gate is the thing doing the work, not the cause-type check --
    pin this with a ceiling error whose cause WOULD satisfy the 404 branch if
    the kind check were ever loosened or removed."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/sessions/sess_1/events")
    response = httpx.Response(404, request=request, json={"error": {"message": "not found"}})
    cause = anthropic.APIStatusError("not found", response=response, body=None)
    state = TurnState(error=TurnError(kind="ceiling", message="abandoned", cause=cause))

    assert _is_dead_session(state) is False
