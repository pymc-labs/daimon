"""Shared turn-admission gate -- pure, platform-agnostic (SCORE-04)."""

from __future__ import annotations


def should_admit_turn(*, current_in_flight: int, cap: int) -> bool:
    """Per-tenant concurrency gate. Returns True when the tenant has capacity.

    Pure function -- no I/O, no state. Shared by Discord and the future Slack
    adapter. Called at message admission before incrementing the in-flight
    counter.
    """
    return current_in_flight < cap
