"""The scheduler's per-fire dispatch timeout, read by the scheduler and by a routine move."""

from __future__ import annotations

from daimon.core.routine_move import RoutineFreshnessSettings
from daimon.core.turn.ceiling import LONGEST_TURN_CEILING_S
from pydantic import Field, field_validator

# Headroom above the longest core turn ceiling for the pre-turn adapter work
# `_fire` does OUTSIDE headless_runner's two ceiling-covered legs (routine
# row bookkeeping, agent/environment resolution, the usage-recorder factory,
# `record_result`). `dispatch_timeout_s`'s default is derived from this
# constant plus `LONGEST_TURN_CEILING_S` rather than hardcoded, so the inversion
# 19-VERIFICATION.md found (an outer guard sitting BELOW the core ceiling
# it's supposed to backstop) is structurally impossible to reintroduce.
DISPATCH_TIMEOUT_MARGIN_S: float = 300.0


class DispatchTimeoutSettings(RoutineFreshnessSettings):
    dispatch_timeout_s: float = Field(
        default=LONGEST_TURN_CEILING_S + DISPATCH_TIMEOUT_MARGIN_S,
        description=(
            "An OUTER PROCESS GUARD (asyncio.wait_for), not the turn's "
            "deadline. The core turn ceiling (daimon.core.turn.ceiling: 45 "
            "minutes, or less by the unattended run limit) "
            "is enforced inside headless_runner.run_turn and fires first, "
            "producing a TurnError(kind='ceiling'). This bound only catches a "
            "fire that hangs OUTSIDE the ceiling's two legs (routine row "
            "bookkeeping, agent/environment resolution, the usage-recorder "
            "factory, record_result), and must stay strictly above "
            "LONGEST_TURN_CEILING_S or the core ceiling becomes unreachable for "
            "routines. Each routine has its own timeout; continuous scheduler "
            "ticks do not await in-flight routines. Missed slots follow the "
            "routine's catch_up_policy."
        ),
    )

    @field_validator("dispatch_timeout_s")
    @classmethod
    def _above_turn_ceiling(cls, value: float) -> float:
        if value <= LONGEST_TURN_CEILING_S:
            raise ValueError(f"must be above LONGEST_TURN_CEILING_S ({LONGEST_TURN_CEILING_S}s)")
        return value
