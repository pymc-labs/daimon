"""Record elapsed time from setup start to the first CLI text delta.

Usage:
  python3 scripts/measure_first_reply.py start --method agent
  python3 scripts/measure_first_reply.py observe --method agent --human-steps 1 -- COMMAND ...

The state file records timestamps only. The observe command passes the CLI's
NDJSON through unchanged and writes one metric JSON object to stderr.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_STATE = Path(".daimon-setup-timing.json")


def _first_text(line: str) -> bool:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or payload.get("kind") != "sse":
        return False
    event = payload.get("event")
    if not isinstance(event, dict):
        return False
    if event.get("type") == "agent.message":
        content = event.get("content")
        return isinstance(content, list) and any(
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and bool(part["text"])
            for part in content
        )
    delta = event.get("delta")
    return (
        isinstance(delta, dict)
        and delta.get("type") == "text_delta"
        and isinstance(delta.get("text"), str)
        and bool(delta["text"])
    )


def _terminal_status(line: str) -> str | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("kind") == "terminal":
        status = payload.get("status")
        return status if isinstance(status, str) else None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "observe"))
    parser.add_argument("--method", choices=("agent", "manual"), required=True)
    parser.add_argument("--human-steps", type=int, default=0)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    argv = sys.argv[1:]
    split_at = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split_at])
    command = argv[split_at + 1 :] if split_at < len(argv) else []
    if args.action == "start":
        if command:
            parser.error("start takes no command")
        started = time.time()
        args.state_file.write_text(json.dumps({"method": args.method, "started_at": started}))
        print(json.dumps({"method": args.method, "started_at_utc": _utc(started)}))
        return 0
    if args.human_steps < 0:
        parser.error("--human-steps must be nonnegative")
    if not command:
        parser.error("observe requires a command after --")
    try:
        state = json.loads(args.state_file.read_text())
        started = float(state["started_at"])
    except (OSError, KeyError, ValueError, TypeError) as exc:
        parser.error(f"missing or invalid timer state: {exc}")
    if state.get("method") != args.method:
        parser.error("--method differs from the recorded start")

    first_text_at: float | None = None
    terminal: str | None = None
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, bufsize=1) as process:
        assert process.stdout is not None
        for line in process.stdout:
            observed = time.time()
            sys.stdout.write(line)
            sys.stdout.flush()
            if first_text_at is None and _first_text(line):
                first_text_at = observed
            terminal = _terminal_status(line) or terminal
        return_code = process.wait()

    finished = time.time()
    metric = {
        "method": args.method,
        "started_at_utc": _utc(started),
        "first_text_at_utc": _utc(first_text_at) if first_text_at is not None else None,
        "seconds_to_first_text": round(first_text_at - started, 3)
        if first_text_at is not None
        else None,
        "seconds_to_terminal": round(finished - started, 3),
        "human_steps": args.human_steps,
        "terminal_status": terminal,
        "command_exit_code": return_code,
    }
    print(json.dumps(metric, sort_keys=True), file=sys.stderr)
    args.state_file.unlink(missing_ok=True)
    return return_code if return_code else (0 if terminal == "end_turn" and first_text_at else 1)


def _utc(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
