#!/usr/bin/env bash
# Offline: no Azure CLI, credentials, subscription, or resource creation.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
if ! command -v bicep >/dev/null 2>&1; then
  echo 'Install the standalone Bicep CLI and add it to PATH.' >&2
  exit 1
fi
build_dir=$(mktemp -d)
trap 'rm -rf -- "$build_dir"' EXIT
while IFS= read -r template; do
  relative=${template#"$repo_root/infra/azure/"}
  mkdir -p -- "$build_dir/$(dirname -- "$relative")"
  if ! bicep build "$template" --outfile "$build_dir/${relative%.bicep}.json" --diagnostics-format sarif > "$build_dir/build.sarif" 2>&1; then
    cat "$build_dir/build.sarif" >&2
    exit 1
  fi
  if ! bicep lint "$template" --diagnostics-format sarif > "$build_dir/lint.sarif" 2>&1; then
    cat "$build_dir/lint.sarif" >&2
    exit 1
  fi
  # Bicep returns success on warnings. Require zero warnings as well as zero errors.
  python3 - "$build_dir/build.sarif" "$build_dir/lint.sarif" <<'PY'
import json
import sys

for path in sys.argv[1:]:
    with open(path) as stream:
        runs = json.load(stream)['runs']
    for run in runs:
        for result in run.get('results', []):
            if result.get('level', 'warning') in ('warning', 'error'):
                print(result['message']['text'], file=sys.stderr)
                sys.exit(1)
PY
  echo "Validated $relative"
done < <(find "$repo_root/infra/azure" -name '*.bicep' -type f | sort)
