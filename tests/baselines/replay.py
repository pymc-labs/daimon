"""M0 paired replay overhead measurement; pending until the N4 bridge adapter lands."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

TurnPath = Literal["legacy", "mux"]


@dataclass(frozen=True)
class Evidence:
    selected_path: TurnPath
    effect_digest: str
    transport_calls: int
    first_event_ms: float
    external_calls: int = 0


class ReplayAdapter(Protocol):
    """N4 supplies a fresh deterministic transport replay, no sleeps/provider I/O.

    replay(path) must set DAIMON_TURN__PATH to path before constructing the
    bridge/settings, restore the environment afterwards, and return the
    path actually selected. Reset fixture state per invocation. Digest the
    normalized recorded effects identically on both paths. Measure first_event_ms
    from turn entry to the first normalized event under the fake transport.
    """

    @property
    def offline(self) -> bool: ...
    async def replay(self, path: TurnPath) -> Evidence: ...


def percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered or not 0 <= quantile <= 1:
        raise ValueError("percentile needs samples and a quantile in [0,1]")
    index = (len(ordered) - 1) * quantile
    low = math.floor(index)
    high = math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


async def measure(
    adapter: ReplayAdapter | None,
    *,
    iterations: int = 200,
    warmups: int = 20,
    clock: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, object]:
    if adapter is None:
        return {
            "status": "pending",
            "reason": "N4 legacy/mux turn bridge and transport replay adapter have not landed",
            "p50_added_ms": None,
            "p95_added_ms": None,
            "gate_passed": None,
        }
    if not adapter.offline:
        raise ValueError("only an offline transport fake is allowed")
    if iterations < 2 or warmups < 0:
        raise ValueError("need at least two iterations and nonnegative warmups")
    samples: list[float] = []
    legacy: list[float] = []
    mux: list[float] = []
    first_events: dict[TurnPath, list[float]] = {"legacy": [], "mux": []}
    first_added: list[float] = []
    expected: tuple[str, int] | None = None
    for index in range(warmups + iterations):
        paths: tuple[TurnPath, TurnPath] = (
            ("legacy", "mux") if index % 2 == 0 else ("mux", "legacy")
        )
        durations: dict[TurnPath, float] = {}
        first_pair: dict[TurnPath, float] = {}
        for path in paths:
            start = clock()
            evidence = await adapter.replay(path)
            elapsed = (clock() - start) / 1_000_000
            if elapsed < 0:
                raise ValueError("non-monotonic benchmark clock")
            if evidence.selected_path != path or evidence.external_calls != 0:
                raise ValueError("wrong turn path or external I/O in offline replay")
            if (
                not math.isfinite(evidence.first_event_ms)
                or not 0 <= evidence.first_event_ms <= elapsed
            ):
                raise ValueError("first-event latency must be observed within the replay duration")
            first_pair[path] = evidence.first_event_ms
            identity = (evidence.effect_digest, evidence.transport_calls)
            if not evidence.effect_digest or evidence.transport_calls < 1:
                raise ValueError("replay needs normalized effects and fake transport calls")
            if expected is None:
                expected = identity
            elif identity != expected:
                raise ValueError("legacy/mux replay effects or transport work differ")
            durations[path] = elapsed
        if index >= warmups:
            samples.append(durations["mux"] - durations["legacy"])
            legacy.append(durations["legacy"])
            mux.append(durations["mux"])
            for path in ("legacy", "mux"):
                first_events[path].append(first_pair[path])
            first_added.append(first_pair["mux"] - first_pair["legacy"])
    p50 = percentile(samples, 0.5)
    p95 = percentile(samples, 0.95)
    return {
        "status": "measured",
        "measured_at": datetime.now(UTC).isoformat(),
        "iterations": iterations,
        "warmups": warmups,
        "p50_added_ms": p50,
        "p95_added_ms": p95,
        "legacy_p50_ms": percentile(legacy, 0.5),
        "mux_p50_ms": percentile(mux, 0.5),
        "first_event_ms": {
            path: {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}
            for path, values in first_events.items()
        },
        "first_event_added_ms": {
            "p50": percentile(first_added, 0.5),
            "p95": percentile(first_added, 0.95),
        },
        "limits_ms": {"p50": 5, "p95": 20},
        "gate_passed": p50 <= 5 and p95 <= 20,
        "effect_digest": expected[0] if expected else None,
    }


def main() -> None:
    import asyncio

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adapter", help="N4 offline adapter module:factory; omitted yields pending"
    )
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmups", type=int, default=20)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    adapter = None
    if args.adapter:
        module, factory = args.adapter.rsplit(":", 1)
        adapter = cast(ReplayAdapter, getattr(importlib.import_module(module), factory)())
    result = asyncio.run(measure(adapter, iterations=args.iterations, warmups=args.warmups))
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
