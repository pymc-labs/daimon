#!/usr/bin/env bash
# Compare documentation only; never execute the extracted Azure commands.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
python3 - "${1:-$repo/deploy/helm/daimon-selfhost/INSTALL-COKE.md}" "${2:-$repo/deploy/helm/daimon-selfhost/README.md}" <<'PY'
import re
import sys
from pathlib import Path

kit_path, recipe_path = map(Path, sys.argv[1:])
kit, recipe = kit_path.read_text(), recipe_path.read_text()
lines = kit.splitlines()
if len(lines) > 60:
    sys.exit(f"Install kit exceeds 60 lines: {len(lines)}")


def commands(document):
    result = []
    for language, block in re.findall(r"^```([^\n]*)\n(.*?)^```\s*$", document, re.M | re.S):
        if language not in {"sh", "bash", "shell"}:
            continue
        pending = []
        for line in block.splitlines():
            if not pending and (not line.strip() or line.lstrip().startswith("#")):
                continue
            pending.append(line)
            if not line.endswith("\\"):
                result.append("\n".join(pending))
                pending = []
        if pending:
            sys.exit("Unfinished command continuation")
    return result


extracted, documented = commands(kit), commands(recipe)
if not extracted:
    sys.exit("Install kit has no shell commands")
missing = [command for command in extracted if command not in documented]
if missing:
    sys.exit("Install kit commands missing verbatim from recipe:\n" + "\n".join(missing))
print(f"PASS: {len(extracted)} commands match recipe verbatim; {len(lines)}/60 Markdown lines")
PY
