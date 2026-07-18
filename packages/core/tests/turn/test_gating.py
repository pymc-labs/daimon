"""Tests for the shared turn-admission gate (daimon.core.turn.gating).

Behavior spec:
  - should_admit_turn(current_in_flight=N, cap=M) -> True when N < M
  - should_admit_turn(current_in_flight=N, cap=M) -> False when N >= M
"""

from __future__ import annotations

from daimon.core.turn.gating import should_admit_turn


def test_should_admit_turn_admits_under_cap() -> None:
    assert should_admit_turn(current_in_flight=2, cap=3) is True, (
        "turn count under cap should be admitted"
    )


def test_should_admit_turn_rejects_at_cap() -> None:
    assert should_admit_turn(current_in_flight=3, cap=3) is False, (
        "turn count at cap should be rejected"
    )


def test_should_admit_turn_rejects_over_cap() -> None:
    assert should_admit_turn(current_in_flight=4, cap=3) is False, (
        "turn count over cap should be rejected"
    )
