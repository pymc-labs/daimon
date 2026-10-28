"""Copy the sandbox I/O helper into independently delivered default skills."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "defaults" / "skills"
SOURCE = ROOT / "workspace-write" / "scripts" / "workflow_io.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = SOURCE.read_bytes()
    for name in ("slides-workflow", "toggl-edits", "image-workflow"):
        folder = ROOT / name / "scripts"
        if not folder.exists():
            continue
        target = folder / SOURCE.name
        if args.check:
            if not target.is_file() or target.read_bytes() != data:
                print(f"out of date: {target.relative_to(ROOT)}")
                return 1
        else:
            target.write_bytes(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
