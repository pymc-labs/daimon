#!/usr/bin/env bash
# Guard the public repo against leaking companion-repo material.
#
# This repo is public. A handful of things must never appear in it: the name of
# the companion planning repo, production hostnames and cloud project ids, paths
# into the planning directory (symlinked in and gitignored), and internal phase
# designators.
#
# Deliberately NOT checked: threat-model ids (T-nn-nn), decision ids (D-nn), and
# requirement ids (SYNC-nn, SUITE-nn, ...). Those are an established convention
# here -- ~195 occurrences across ~79 files -- and the threat-model references in
# particular are load-bearing security documentation. Banning them would delete
# traceability, not protect anything.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

# Trees to scan. This script is excluded from its own scan (it names the patterns).
readonly SELF="scripts/lint_designators.sh"
readonly TREES=(packages apps scripts .github defaults)

failed=0

# report <rule> <explanation> <extended-regex>
report() {
    local rule="$1" why="$2" pattern="$3" hits
    # $4 optionally narrows the scan to one tree (a rule that only applies there).
    local -a scope=("${TREES[@]}")
    [[ $# -ge 4 ]] && scope=("$4")
    hits=$(grep -rnE "$pattern" "${scope[@]}" 2>/dev/null | grep -v "^${SELF}:") || true
    if [[ -n "$hits" ]]; then
        echo "FAIL [$rule] $why"
        echo "$hits" | sed 's/^/    /'
        echo
        failed=1
    fi
}

report "tenant-branding-in-defaults" \
    "defaults/ is the product every operator inherits, not one operator's config; PyMC Labs branding belongs in the private tenant skill repo, delivered by repo binding" \
    'PyMC Labs|#?(0C1F40|9FAAE2|B4E7DD|F6AE72|006FFF|1e3a5f)\b|Archivo' \
    defaults

report "private-repo-name" \
    "the companion planning repo must not be named in public code; say \"the companion planning repo\"" \
    'daimon-private'

report "production-identifiers" \
    "production hostnames and cloud project ids belong in config or the companion repo, not in source" \
    'daimon\.decision\.ai|pymc-daimon-prod'

report "planning-paths" \
    "the planning directory is gitignored here; code must not reference paths inside it" \
    '\.planning/'

report "phase-designator" \
    "internal phase numbers are meaningless outside the companion repo; describe the behavior instead" \
    '\bPhase [0-9]'

if [[ "$failed" -ne 0 ]]; then
    echo "lint_designators: found material that must not appear in the public repo."
    echo "Fix the lines above. Do not add exemptions to work around them."
    exit 1
fi

echo "lint_designators: 4 rules clean"
