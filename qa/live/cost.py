"""Shared UTC daily cap, crash-persistent reservations, and per-run receipts."""

from __future__ import annotations

import fcntl
import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast

from pydantic import JsonValue

from qa.live.config import Pricing
from qa.live.schema import Scenario
from qa.live.types import Usage, utcnow

DAILY_CAP = 10.0


def estimate(scenario: Scenario, pricing: Pricing) -> float:
    judges = sum(a.kind == "judge" for a in scenario.assertions)
    return (
        scenario.est_turns * pricing.per_turn_usd
        + judges
        * (
            pricing.judge_input_token_limit * pricing.judge_input_per_million
            + 300 * pricing.judge_output_per_million
        )
        / 1_000_000
    )


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.reservations = path.with_suffix(path.suffix + ".reservations.json")

    @contextmanager
    def locked(self) -> Iterator[TextIO]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield lock
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _pending(self) -> dict[str, dict[str, JsonValue]]:
        if not self.reservations.exists():
            return {}
        return cast(dict[str, dict[str, JsonValue]], json.loads(self.reservations.read_text()))

    def _save_pending(self, pending: dict[str, dict[str, JsonValue]]) -> None:
        temporary = self.reservations.with_suffix(".tmp")
        temporary.write_text(json.dumps(pending))
        temporary.replace(self.reservations)

    def _today(self) -> float:
        today = utcnow().date()
        total = 0.0
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if not line.strip():
                    continue
                row = cast(dict[str, JsonValue], json.loads(line))
                stamp = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("ledger timestamps require UTC offset")
                cost = float(str(row["usd"]))
                if not math.isfinite(cost) or cost < 0:
                    raise ValueError("ledger cost must be finite and nonnegative")
                if stamp.astimezone(UTC).date() == today:
                    total += cost
        for row in self._pending().values():
            # Reservations do not expire automatically: a crashed run may have spent money.
            total += float(str(row["usd"]))
        return total

    def reserve(self, run_id: str, usd: float) -> None:
        if not math.isfinite(usd) or usd < 0:
            raise ValueError("invalid run estimate")
        with self.locked():
            pending = self._pending()
            if run_id in pending:
                raise ValueError("duplicate reservation")
            if self._today() + usd > DAILY_CAP:
                raise ValueError("QA daily $10 cap would be exceeded")
            pending[run_id] = {"ts": utcnow().isoformat(), "usd": usd}
            self._save_pending(pending)

    def charged(self, run_ids: set[str]) -> float:
        """Final receipts only; concurrent reservation releases are not credits."""
        total = 0.0
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if not line.strip():
                    continue
                row = cast(dict[str, JsonValue], json.loads(line))
                if str(row.get("run_id")) in run_ids:
                    cost = float(str(row["usd"]))
                    if not math.isfinite(cost) or cost < 0:
                        raise ValueError("invalid final receipt cost")
                    total += cost
        return total

    def claim_schedule(self, mode: str, env: str) -> None:
        path = self.path.with_suffix(".schedule.json")
        with self.locked():
            state = cast(dict[str, str], json.loads(path.read_text())) if path.exists() else {}
            key = f"canary:{env}" if mode == "canary" else "catalog"
            now = utcnow()
            if key in state:
                previous = datetime.fromisoformat(state[key])
                if (
                    mode == "canary"
                    and previous.replace(minute=0, second=0, microsecond=0)
                    == now.replace(minute=0, second=0, microsecond=0)
                ) or (mode != "canary" and previous.astimezone(UTC).date() == now.date()):
                    raise ValueError("Scenario B cadence already claimed for this interval")
            state[key] = now.isoformat()
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state))
            temporary.replace(path)

    def receipt(
        self, run_id: str, usages: list[Usage], estimated: float, *, spend_possible: bool = True
    ) -> None:
        if any(u.usd is not None and (not math.isfinite(u.usd) or u.usd < 0) for u in usages):
            raise ValueError("measured cost must be finite and nonnegative")
        measured = sum(u.usd or 0.0 for u in usages)
        complete = bool(usages) and all(
            u.usd is not None and u.source in {"turn_outcomes", "anthropic_messages"}
            for u in usages
        )
        no_spend = not spend_possible and not usages
        charged = 0.0 if no_spend else (measured if complete else max(estimated, measured))
        models = sorted({model for usage in usages for model in usage.models})
        row = {
            "ts": utcnow().isoformat(),
            "who": "qa-runner",
            "model": models[0] if len(models) == 1 else ("multiple" if models else "unavailable"),
            "models": models,
            "run_id": run_id,
            "usd": charged,
            "actual_usd": measured if complete or no_spend else None,
            "accounting": "no_trigger"
            if no_spend
            else ("actual" if complete else "conservative_estimate"),
            "input_tokens": sum(u.input_tokens or 0 for u in usages),
            "output_tokens": sum(u.output_tokens or 0 for u in usages),
            "cache_read_input_tokens": sum(u.cache_read_input_tokens or 0 for u in usages),
            "cache_creation_input_tokens": sum(u.cache_creation_input_tokens or 0 for u in usages),
            "usage": [asdict(u) for u in usages],
        }
        with self.locked():
            pending = self._pending()
            if run_id not in pending:
                raise ValueError("receipt requires an existing reservation")
            with self.path.open("a") as ledger:
                ledger.write(json.dumps(row) + "\n")
                ledger.flush()
            del pending[run_id]
            self._save_pending(pending)
