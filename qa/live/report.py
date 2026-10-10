"""JSON evidence, five-line summaries, and deduplicated handoff alerts."""

from __future__ import annotations

import fcntl
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from qa.live.config import Alerts
from qa.live.errors import redact
from qa.live.schema import Status
from qa.live.types import Check, Message, Turn, utcnow


@dataclass
class Result:
    run_id: str
    scenario: str
    env: str
    status: Status = "PENDING"
    checks: list[Check] = field(default_factory=list[Check])
    turns: list[Turn] = field(default_factory=list[Turn])
    notes: list[str] = field(default_factory=list[str])
    channel_id: str | None = None
    errors: list[Message] = field(default_factory=list[Message])

    def finalize(self) -> None:
        statuses = [c.status for c in self.checks]
        self.status = (
            "FAIL"
            if "FAIL" in statuses
            else ("PASS" if statuses and all(s == "PASS" for s in statuses) else "PENDING")
        )


def report(result: Result, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.run_id}.json"
    path.write_text(json.dumps(asdict(result), default=str, indent=2) + "\n")
    print(
        "\n".join(
            [
                f"run: {result.run_id}",
                f"scenario: {result.scenario}",
                f"environment: {result.env}",
                f"result: {result.status} ({len(result.checks)} checks)",
                f"evidence: {path}",
            ]
        )
    )
    return path


class Alerter:
    def __init__(self, config: Alerts, state_path: Path) -> None:
        self.config = config
        self.state_path = state_path

    def notify(self, result: Result, evidence_path: Path) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._notify(result, evidence_path)

    def _notify(self, result: Result, evidence_path: Path) -> None:
        state = (
            cast(dict[str, dict[str, str]], json.loads(self.state_path.read_text()))
            if self.state_path.exists()
            else {}
        )
        key = f"{result.env}:{result.scenario}"
        prior = state.get(key, {})
        now = utcnow()
        pending_count = (
            int(prior.get("pending_count", "0")) + 1 if result.status == "PENDING" else 0
        )
        if result.status == "PENDING":
            prior["pending_count"] = str(pending_count)
            state[key] = prior
            if pending_count < self.config.pending_threshold:
                self._save_state(state)
                return
        recovery = result.status == "PASS" and prior.get("status") in {"FAIL", "PENDING"}
        if result.status == "PASS" and not recovery:
            prior["pending_count"] = "0"
            state[key] = prior
            self._save_state(state)
            return
        if result.status in {"FAIL", "PENDING"} and prior.get("status") in {"FAIL", "PENDING"}:
            previous = datetime.fromisoformat(prior["ts"])
            if (now - previous).total_seconds() < self.config.cooldown_s:
                prior["pending_count"] = str(pending_count)
                self._save_state(state)
                return
        label = "RECOVERY" if recovery else result.status
        line = f"QA canary {label}: {result.scenario} ({result.env}), {evidence_path}"
        if result.status == "PENDING":
            line += f"; {pending_count} consecutive PENDING runs"
        inbox = Path(self.config.inbox)
        inbox.mkdir(parents=True, exist_ok=True)
        failures = "\n".join(
            f"- {c.kind}, turn {c.turn}: {c.reason}\n  Evidence: {', '.join(c.evidence)}"
            for c in result.checks
            if c.status == result.status
        )
        alert_path = inbox / f"qa-canary-{label}-{now.strftime('%Y%m%dT%H%M%S%fZ')}.md"
        alert_path.write_text(
            f"{line}\n\n{failures}\n\nRun evidence: {evidence_path.resolve()}\n",
        )
        # The inbox is the durable delivery channel. A busy tmux composer can
        # leave tsend queued with exit 1; never duplicate that durable notice.
        state[key] = {
            "status": result.status,
            "ts": now.isoformat(),
            "pending_count": str(pending_count),
            "delivery": "durable inbox written; tsend pending",
        }
        self._save_state(state)
        try:
            delivery = subprocess.run(
                [*self.config.command, line],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            status = f"tsend exit {delivery.returncode}"
            if delivery.returncode:
                status += "; delivered-pending via durable inbox"
            detail = redact(str(delivery.stdout or "") + str(delivery.stderr or ""))
        except (OSError, subprocess.TimeoutExpired) as exc:
            status = f"tsend {type(exc).__name__}; delivered-pending via durable inbox"
            detail = ""
        state[key]["delivery"] = status
        self._save_state(state)
        with alert_path.open("a") as stream:
            stream.write(f"\nDelivery: {status}\n{detail}\n")

    def _save_state(self, state: dict[str, dict[str, str]]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(self.state_path)
