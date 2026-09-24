#!/usr/bin/env bash
# Model-check every config listed in expected.tsv and compare TLC's verdict
# with the expected one. A deliberately unsafe config must still produce its
# counterexample; a fixed or safe config must stay clean. Any other verdict,
# including a parse, semantic or deadlock error, fails the run.
#
# Usage: formal/check.sh
# Needs java (17+) and TLA2TOOLS_JAR (default: formal/tla2tools.jar), the
# tla2tools v1.7.4 release pinned in formal/README.md.
#
# Each row also pins TLC's distinct-state count. A mismatch fails the run, and
# so does a count of 0 or a missing count: a model whose Init is empty or whose
# Next is over-constrained still reports "No error has been found", so the
# verdict alone would pass it as clean without checking anything.
#
# Verdicts: clean | violates:<Invariant> | violates:<Property> | deadlock | error.
# TLC reports a temporal violation without naming the property, so the name is
# taken from the config's PROPERTY/PROPERTIES list, which must then hold exactly
# one property; otherwise the verdict is violates:temporal(<p1>|<p2>...).
# One worker keeps the state counts reproducible, which is what lets them be
# pinned; a parallel run stops at a scheduling-dependent point on a violation.
set -uo pipefail
cd "$(dirname "$0")"
JAR=${TLA2TOOLS_JAR:-$PWD/tla2tools.jar}
if [[ ! -f "$JAR" ]]; then
  echo "tla2tools.jar not found at $JAR (set TLA2TOOLS_JAR)" >&2
  exit 2
fi
META=$(mktemp -d "${TMPDIR:-/tmp}/daimon-formal-check.XXXXXX")
trap 'rm -rf "$META"' EXIT

# Property names declared in a TLC config (PROPERTY / PROPERTIES sections).
properties() {
  awk '
    /^[[:space:]]*(SPECIFICATION|INIT|NEXT|CONSTANTS?|INVARIANTS?|CHECK_DEADLOCK|SYMMETRY|VIEW|CONSTRAINTS?|ACTION_CONSTRAINTS?|ALIAS|POSTCONDITION)([[:space:]]|$)/ { inprop = 0 }
    /^[[:space:]]*PROPERT(Y|IES)([[:space:]]|$)/ { inprop = 1; sub(/^[[:space:]]*PROPERT(Y|IES)([[:space:]]|$)/, "") }
    inprop { for (i = 1; i <= NF; i++) print $i }
  ' "$1"
}

fail=0
total_start=$SECONDS
while IFS=$'\t' read -r dir spec cfg expect expect_states flags _note; do
  [[ -z "${dir}" || "${dir}" == \#* ]] && continue
  [[ "$flags" == "-" ]] && flags=""
  start=$SECONDS
  # shellcheck disable=SC2086  # flags is a deliberately word-split list
  out=$(cd "$dir" && java -XX:+UseParallelGC -Djava.io.tmpdir="$META" -cp "$JAR" tlc2.TLC -workers 1 ${flags} \
          -metadir "$META/$dir-$cfg" -config "$cfg.cfg" "$spec.tla" 2>&1)
  code=$?
  secs=$((SECONDS - start))
  if [[ $code -eq 0 ]] && grep -q "No error has been found" <<<"$out"; then
    got=clean
  elif [[ $code -eq 12 ]] && inv=$(grep -oE "Invariant [A-Za-z0-9_]+ is violated" <<<"$out" | head -1) && [[ -n "$inv" ]]; then
    got="violates:$(awk '{print $2}' <<<"$inv")"
  elif [[ $code -eq 13 ]] && grep -q "Temporal properties were violated" <<<"$out"; then
    mapfile -t props < <(properties "$dir/$cfg.cfg")
    if [[ ${#props[@]} -eq 1 ]]; then
      got="violates:${props[0]}"
    else
      got="violates:temporal($(IFS='|'; echo "${props[*]}"))"
    fi
  elif [[ $code -eq 11 ]] || grep -q "Deadlock reached" <<<"$out"; then
    got=deadlock
  else
    got="error(exit $code)"
  fi
  states=$(grep -oE "[0-9,]+ distinct states found" <<<"$out" | tail -1 | awk '{print $1}' | tr -d ,)
  problem=""
  if [[ "$got" != "$expect" ]]; then
    problem="expected $expect, got $got"
  elif [[ -z "$states" ]]; then
    problem="no distinct-state count in TLC output"
  elif [[ "$states" == 0 ]]; then
    problem="0 distinct states: nothing was checked"
  elif [[ "$states" != "$expect_states" ]]; then
    problem="expected $expect_states distinct states, got $states"
  fi
  if [[ -z "$problem" ]]; then
    printf 'ok    %-58s %-34s %8s distinct %4ss\n' "$dir/$cfg" "$got" "$states" "$secs"
  else
    printf 'FAIL  %-58s %s (%ss)\n' "$dir/$cfg" "$problem" "$secs"
    tail -30 <<<"$out"
    fail=1
  fi
done < expected.tsv
echo "total $((SECONDS - total_start))s"
exit $fail
