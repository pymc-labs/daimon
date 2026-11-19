"""Per-stage timings for one timing log line (`turn.admission_timing` and its siblings)."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter


def stage_timer(stage_ms: dict[str, float] | None) -> Callable[[str], None]:
    """A `mark(stage)` that adds the ms since the last mark to `stage_ms[stage]`; None: no-op."""
    last = perf_counter()

    def mark(stage: str) -> None:
        nonlocal last
        current = perf_counter()
        if stage_ms is not None:
            stage_ms[stage] = round(stage_ms.get(stage, 0.0) + (current - last) * 1000, 1)
        last = current

    return mark


def elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 1)
