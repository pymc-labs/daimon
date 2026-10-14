"""Explicit F2 live entry, sharing the public default-capability scenario.

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
        live_command("F2-Anthropic", args)
        return
    args.output.mkdir(parents=True)
    (args.output / "receipt.json").write_text(
        json.dumps(
            {
                "status": "PENDING",
                "model": MODEL,
                "native_control_mode": "guard-only",
                "native_budget_sent": False,
                "external_provider_calls": 0,
                "credential_reads": 0,
                "required": [
                    "merged dual-signed entry",
                    "signed installation and separate GO",
                    "real authorized provider-reachable MCP with explicit owned bearer",
                    "disposable scoped environment",
                ],
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
