#!/usr/bin/env bash
# Guard the public repo against private material.
#
# This repo is public. A handful of things must never appear in it: the name of
# the companion planning repo, production hostnames and cloud project ids, paths
# into the planning directory (symlinked in and gitignored), internal phase
# designators, people's home-directory paths, and the names of real customers.
#
# Customer names cannot be listed here without publishing them, so that rule
# reads a private pattern list supplied from outside the repo (see
# "private-denylist" below). Its hits are reported as file:line only, so CI logs
# on this public repo never echo the matched text.
#
# Deliberately NOT checked: threat-model ids (T-nn-nn), decision ids (D-nn), and
# requirement ids (SYNC-nn, SUITE-nn, ...). Those are an established convention
# here -- ~195 occurrences across ~79 files -- and the threat-model references in
# particular are load-bearing security documentation. Banning them would delete
# traceability, not protect anything.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

# This script is excluded from its own scan (it names the patterns).
readonly SELF="scripts/lint_designators.sh"
# Code, config, tests and seeded defaults.
readonly CODE_TREES=(packages apps scripts .github qa defaults tests)
# Everything that ships in the repo, prose included.
readonly ALL_TREES=("${CODE_TREES[@]}" docs deploy formal web README.md CONTRIBUTING.md CHANGELOG.md)
# Generic account names that may appear in /home/<name>/ examples.
readonly GENERIC_HOMES='(claude|daimon|harness|qa|work|user|runner|app|node|ubuntu|me|you|example)'

failed=0

existing() {
    local path
    for path in "$@"; do
        [[ -e "$path" ]] && printf '%s\n' "$path"
    done
}

# scan <grep flags...> -- <trees...>: matching lines, this script excluded.
scan() {
    local flags=()
    while [[ "$1" != "--" ]]; do flags+=("$1"); shift; done
    shift
    mapfile -t trees < <(existing "$@")
    [[ ${#trees[@]} -eq 0 ]] && return 0
    grep -rnI --exclude-dir=node_modules --exclude-dir=.git "${flags[@]}" "${trees[@]}" 2>/dev/null \
        | grep -v "^${SELF}:" || true
}

fail() {
    local rule="$1" why="$2" hits="$3"
    echo "FAIL [$rule] $why"
    echo "$hits" | sed 's/^/    /'
    echo
    failed=1
}

# report <rule> <explanation> <extended-regex> [trees...]
report() {
    local rule="$1" why="$2" pattern="$3" hits
    shift 3
    local trees=("$@")
    [[ ${#trees[@]} -eq 0 ]] && trees=("${CODE_TREES[@]}")
    hits=$(scan -E "$pattern" -- "${trees[@]}")
    [[ -n "$hits" ]] && fail "$rule" "$why" "$hits"
}

report "private-repo-name" \
    "the companion planning repo must not be named in public code; say \"the companion planning repo\"" \
    'daimon-private' "${ALL_TREES[@]}"

report "production-identifiers" \
    "production hostnames and cloud project ids belong in config or the companion repo, not in source" \
    'daimon\.decision\.ai|pymc-daimon-prod'

report "planning-paths" \
    "the planning directory is gitignored here; code must not reference paths inside it" \
    '\.planning/' "${ALL_TREES[@]}"

# defaults/ is excluded: seeded skills use "Phase 1/2" as their own step headings.
report "phase-designator" \
    "internal phase numbers are meaningless outside the companion repo; describe the behavior instead" \
    '\bPhase [0-9]' packages apps scripts .github qa tests

# People's home directories. Generic account names (/home/work/, /home/claude/) are fine.
home_hits=$(scan -E '/(home|Users)/[A-Za-z0-9_.-]+/' -- "${ALL_TREES[@]}" \
    | while IFS= read -r line; do
        rest="${line#*:*:}"
        if grep -oE '/(home|Users)/[A-Za-z0-9_.-]+/' <<<"$rest" \
            | grep -qvE "^/(home|Users)/${GENERIC_HOMES}/$"; then
            printf '%s\n' "$line"
        fi
    done)
[[ -n "$home_hits" ]] && fail "home-directory" \
    "a person's home directory must not appear; use Path.home(), ~ or a generic path such as /home/user/" \
    "$home_hits"

# private-denylist: names that must never appear (real customers, partners).
# One extended regex per line, matched case-insensitively as a whole word;
# blank lines and lines starting with # are ignored. Supplied from outside the
# repo, first match wins:
#   1. DAIMON_LINT_DENYLIST         the list itself (CI: the repo secret of that name)
#   2. DAIMON_LINT_DENYLIST_FILE    a path to the list
#   3. ~/.config/daimon/lint-denylist
# With none of these the rule is skipped with a notice (forks and fresh clones).
denylist=""
denylist_source=""
if [[ -n "${DAIMON_LINT_DENYLIST:-}" ]]; then
    denylist="$DAIMON_LINT_DENYLIST"
    denylist_source="DAIMON_LINT_DENYLIST"
elif [[ -n "${DAIMON_LINT_DENYLIST_FILE:-}" ]]; then
    if [[ ! -r "$DAIMON_LINT_DENYLIST_FILE" ]]; then
        echo "FAIL [private-denylist] DAIMON_LINT_DENYLIST_FILE is set but not readable"
        exit 1
    fi
    denylist=$(<"$DAIMON_LINT_DENYLIST_FILE")
    denylist_source="DAIMON_LINT_DENYLIST_FILE"
elif [[ -r "${HOME:-/nonexistent}/.config/daimon/lint-denylist" ]]; then
    denylist=$(<"$HOME/.config/daimon/lint-denylist")
    denylist_source="~/.config/daimon/lint-denylist"
fi

denylist_status="skipped"
if [[ -n "$denylist" ]]; then
    patterns=$(mktemp)
    trap 'rm -f "$patterns"' EXIT
    printf '%s\n' "$denylist" | tr -d '\r' | grep -vE '^[[:space:]]*(#|$)' >"$patterns" || true
    if [[ -s "$patterns" ]]; then
        # -o keeps the match out of the output; cut keeps file:line only.
        deny_hits=$(scan -i -w -E -o -f "$patterns" -- "${ALL_TREES[@]}" | cut -d: -f1,2 | sort -u)
        [[ -n "$deny_hits" ]] && fail "private-denylist" \
            "a name on the private denylist ($denylist_source) appears at these lines; replace it with a neutral example such as \"Acme\" or \"example-client\"" \
            "$deny_hits"
        denylist_status="checked"
    else
        denylist_status="empty"
    fi
else
    echo "lint_designators: no private denylist supplied (DAIMON_LINT_DENYLIST); customer-name rule skipped"
fi

if [[ "$failed" -ne 0 ]]; then
    echo "lint_designators: found material that must not appear in the public repo."
    echo "Fix the lines above. Do not add exemptions to work around them."
    exit 1
fi

echo "lint_designators: 5 rules clean; private denylist $denylist_status"
