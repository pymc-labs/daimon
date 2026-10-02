#!/usr/bin/env python3
"""Prepare .env before installing Python dependencies: python3 scripts/setup.py."""

from __future__ import annotations

import argparse
import json
import runpy
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate missing local daimon secrets.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    core = (
        Path(__file__).resolve().parents[1]
        / "packages/adapters/cli/daimon/adapters/cli/setup_bootstrap.py"
    )
    run_setup = runpy.run_path(str(core))["run_setup"]
    try:
        payload = run_setup(args.env_file)
    except (OSError, UnicodeError, ValueError):
        payload = {
            "schema_version": 1,
            "status": "error",
            "completed": [],
            "missing": [],
            "next_step": "Choose a readable regular environment file with --env-file.",
            "next_optional": [],
        }
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
