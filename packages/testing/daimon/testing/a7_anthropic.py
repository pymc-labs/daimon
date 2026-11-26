"""Explicit A7 live entry: one scoped mux host turn in a fresh session.

No credential/request/reservation occurs in the default live-plan mode.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from daimon.testing.anthropic_live import MODEL, add_live_arguments, live_command


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("live-plan", "live"), default="live-plan")
    parser.add_argument("--output", type=Path, required=True)
    add_live_arguments(parser)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("evidence directory must be new")
    if args.mode == "live":
        live_command("A7", args)
        return
    args.output.mkdir(parents=True)
    receipt = {"status": "PENDING", "model": MODEL, "external_provider_calls": 0}
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
