"""Replay Discord card write events against CardLifecycle's queue actions.

Input is JSON lines from structlog capture or Cloud Logging's jsonPayload.
Legacy events are counted but cannot establish write ordering.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CardTrace:
    pending: dict[str, str] = field(default_factory=dict)
    dispatched: set[str] = field(default_factory=set)
    terminal_applied: bool = False
    terminal_completed: bool = False
    retired: bool = False
    actions: list[str] = field(default_factory=list)

    def step(self, event: str, item: dict[str, Any]) -> None:
        write_id = str(item.get("write_id"))
        kind = str(item.get("kind"))
        if event == "turn.card_write_issued":
            if write_id in self.pending:
                raise ValueError(f"duplicate write {write_id}")
            if kind == "progress" and (self.terminal_applied or self.retired):
                raise ValueError("progress issued after terminal card")
            self.pending[write_id] = kind
            self.actions.append("EndTurn" if kind == "terminal" else "QueueProgress")
        elif event == "turn.card_write_dispatched":
            if self.pending.get(write_id) != kind:
                raise ValueError(f"dispatch without issue: {write_id}")
            if kind == "terminal" and any(
                self.pending[other] == "progress" and other in self.dispatched
                for other in self.pending
            ):
                raise ValueError("terminal dispatched while progress is on wire")
            if kind == "progress" and (self.terminal_applied or self.retired):
                raise ValueError("progress dispatched after terminal card")
            self.dispatched.add(write_id)
            self.actions.append("IssueQueuedTerminal" if kind == "terminal" else "IssueProgress")
        elif event in {"turn.card_write_completed", "turn.card_write_dropped"}:
            if write_id not in self.pending:
                if event == "turn.card_write_dropped" and item.get("reason") == "sealed_or_stale":
                    self.actions.append("DropStale")
                    return
                raise ValueError(f"completion without issue: {write_id}")
            was_dispatched = write_id in self.dispatched
            if event.endswith("completed") and not was_dispatched:
                raise ValueError(f"completion without dispatch: {write_id}")
            if (
                event.endswith("completed")
                and kind == "progress"
                and (self.terminal_applied or self.retired)
            ):
                raise ValueError("progress completed after terminal card")
            if event.endswith("completed") and kind == "terminal":
                self.terminal_completed = True
            self.pending.pop(write_id)
            self.dispatched.discard(write_id)
            self.actions.append("ApplyProgress" if kind == "progress" else "ApplyTerminal")
        elif event == "turn.card_terminal_applied":
            if not self.terminal_completed:
                raise ValueError("terminal applied without completed terminal edit")
            self.terminal_applied = True
            self.actions.append("TerminalVisible")
        elif event == "turn.card_recovery_handover":
            if self.dispatched:
                raise ValueError("handover while card write remains on wire")
            self.terminal_applied = False
            self.terminal_completed = False
            self.actions.append("Handover")
        elif event == "turn.card_repair_scheduled":
            self.actions.append("Repair")
        elif event == "turn.card_orphan_retirement_issued":
            self.actions.append("Crash")
        elif event == "turn.card_orphan_retirement_completed":
            self.retired = True
            self.actions.append("RetireOrphan")
        elif event == "turn.card_orphan_retirement_dropped":
            self.actions.append("DropOrphanRetirement")


EVENTS = {
    "turn.card_write_issued",
    "turn.card_write_dispatched",
    "turn.card_write_completed",
    "turn.card_write_dropped",
    "turn.card_terminal_applied",
    "turn.card_recovery_handover",
    "turn.card_repair_scheduled",
    "turn.card_orphan_retirement_issued",
    "turn.card_orphan_retirement_completed",
    "turn.card_orphan_retirement_dropped",
}


def replay(lines: list[str]) -> tuple[int, int, int]:
    traces: dict[str, CardTrace] = {}
    legacy = 0
    events = 0
    for number, line in enumerate(lines, 1):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            legacy += bool(line.strip())
            continue
        if isinstance(item, dict) and isinstance(item.get("jsonPayload"), dict):
            item = item["jsonPayload"]
        if not isinstance(item, dict):
            legacy += 1
            continue
        event = str(item.get("event", item.get("message", ""))).split(" ", 1)[0]
        if event not in EVENTS:
            legacy += 1
            continue
        key = str(item.get("turn_id") or item.get("message_id") or "")
        if not key:
            raise ValueError(f"line {number}: card event has no turn or message id")
        try:
            traces.setdefault(key, CardTrace()).step(event, item)
        except ValueError as err:
            raise ValueError(f"line {number}, {key}: {err}") from err
        events += 1
    return len(traces), events, legacy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="JSON-lines log file; use - for stdin")
    args = parser.parse_args()
    lines = sys.stdin.readlines() if str(args.log) == "-" else args.log.read_text().splitlines()
    traces, events, legacy = replay(lines)
    print(
        f"accepted {traces}/{traces} replayable traces ({events} events); "
        f"skipped {legacy} legacy lines"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
