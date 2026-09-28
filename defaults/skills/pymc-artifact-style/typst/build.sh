#!/usr/bin/env bash
# Compile a PyMC Labs report with the bundled brand fonts (Inter, JetBrains Mono,
# Fira Math). Run from anywhere. Usage: ./build.sh report.typ [out.pdf]
set -euo pipefail
cd "$(dirname "$0")"
SRC="${1:-starter.typ}"
OUT="${2:-${SRC%.typ}.pdf}"
typst compile "$SRC" "$OUT" --root ".." --font-path "../fonts"
echo "→ $OUT"
