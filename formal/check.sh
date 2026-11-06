#!/usr/bin/env bash
# Model-check every config listed in expected.tsv and compare TLC's verdict
# with the expected one. A deliberately unsafe config must still produce its
# counterexample; a fixed or safe config must stay clean. Any other verdict,
# including a parse, semantic or deadlock error, fails the run.
#
# Usage: formal/check.sh
# Set FORMAL_CHECK_FILTER to part of a registry directory or dir/config (for
# example, github_connect_cancel) to run only the matching configs.
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
# One TLC worker per config keeps the state counts reproducible, which is what
# lets them be pinned; a multi-worker run stops at a scheduling-dependent point
# on a violation. Configs are independent, so up to TLC_JOBS of them run at the
# same time (default: the CPU count, at most 4). Each is still a single-worker
# TLC, so the verdicts and state counts do not depend on TLC_JOBS. Each config
# prints one report when it finishes, so reports come in completion order.
set -uo pipefail
cd "$(dirname "$0")"
JAR=${TLA2TOOLS_JAR:-$PWD/tla2tools.jar}
if [[ ! -f "$JAR" ]]; then
  echo "tla2tools.jar not found at $JAR (set TLA2TOOLS_JAR)" >&2
  exit 2
fi
META=$(mktemp -d "${TMPDIR:-/tmp}/daimon-formal-check.XXXXXX") || exit 2
trap 'rm -rf "$META"' EXIT

# Run one TLC check under a disk and time guard. A model whose state space is
# larger than intended grows its metadir without bound (one local run filled a
# host disk with 16G). TLC is killed when the config's metadir passes
# TLC_MAX_META_MB (default 2048) or the run passes TLC_TIMEOUT_S (default 480,
# under the CI job limit), and each config's metadir is deleted as soon as its
# run ends. Needs GNU coreutils (timeout, du). Sets TLC_KILL_REASON when the
# guard ended the run, so the caller can report why the row failed.
# usage: run_tlc <workdir> <metadir> <outfile> <java args...>  (pass -metadir <metadir>/m)
TLC_MAX_META_MB=${TLC_MAX_META_MB:-2048}
TLC_TIMEOUT_S=${TLC_TIMEOUT_S:-480}
for _var in TLC_MAX_META_MB TLC_TIMEOUT_S; do
  if ! [[ "${!_var}" =~ ^[1-9][0-9]*$ ]]; then
    echo "$_var must be a positive whole number (megabytes / seconds), got '${!_var}'" >&2
    exit 2
  fi
done
TLC_JOBS=${TLC_JOBS:-$(( $(nproc) < 4 ? $(nproc) : 4 ))}
if ! [[ "$TLC_JOBS" =~ ^[1-9][0-9]*$ ]]; then
  echo "TLC_JOBS must be a positive whole number, got '$TLC_JOBS'" >&2
  exit 2
fi
# Several JVMs share the machine, so each gets an equal part of 75% of the RAM
# instead of the JVM default of 25% each, which four of them would exhaust.
JAVA_HEAP=()
(( TLC_JOBS > 1 )) && JAVA_HEAP=("-XX:MaxRAMPercentage=$((75 / TLC_JOBS))")
TLC_KILL_REASON=""
run_tlc() {
  local dir=$1 meta=$2 outfile=$3; shift 3
  TLC_KILL_REASON=""
  mkdir -p "$meta"
  : >"$outfile"
  # Append mode, so a guard message written after TLC's last line is not
  # overwritten by TLC's own file offset. --foreground keeps java in this
  # script's process group, so Ctrl-C still reaches it; -k escalates to KILL
  # if the JVM ignores TERM.
  (cd "$dir" && exec timeout --foreground -k 30 "$TLC_TIMEOUT_S" \
      java -XX:+UseParallelGC "${JAVA_HEAP[@]}" -Djava.io.tmpdir="$meta" "$@") >>"$outfile" 2>&1 &
  local pid=$! mb ticks=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 0.2
    ticks=$((ticks + 1))
    (( ticks % 25 )) && continue  # du about every 5 s; exit is noticed within 0.2 s
    mb=$(du -sm "$meta" 2>/dev/null | cut -f1)
    if [[ "${mb:-0}" -gt "$TLC_MAX_META_MB" ]]; then
      TLC_KILL_REASON="DISK GUARD: metadir ${mb}M > ${TLC_MAX_META_MB}M"
      pkill -P "$pid" 2>/dev/null; kill "$pid" 2>/dev/null
      break
    fi
  done
  wait "$pid"; local code=$?
  if [[ -z "$TLC_KILL_REASON" && $code -eq 124 ]]; then
    TLC_KILL_REASON="TIMEOUT after ${TLC_TIMEOUT_S}s"
  fi
  [[ -n "$TLC_KILL_REASON" ]] && echo "$TLC_KILL_REASON, TLC killed" >>"$outfile"
  rm -rf "$meta"
  return $code
}

# Property names declared in a TLC config (PROPERTY / PROPERTIES sections).
properties() {
  awk '
    /^[[:space:]]*(SPECIFICATION|INIT|NEXT|CONSTANTS?|INVARIANTS?|CHECK_DEADLOCK|SYMMETRY|VIEW|CONSTRAINTS?|ACTION_CONSTRAINTS?|ALIAS|POSTCONDITION)([[:space:]]|$)/ { inprop = 0 }
    /^[[:space:]]*PROPERT(Y|IES)([[:space:]]|$)/ { inprop = 1; sub(/^[[:space:]]*PROPERT(Y|IES)([[:space:]]|$)/, "") }
    inprop { for (i = 1; i <= NF; i++) print $i }
  ' "$1"
}

# Check one expected.tsv row. Writes the report to $RESULTS/<n>.report and then
# "ok" or "fail" to $RESULTS/<n>.status; the status file marks the report as
# complete. Runs in a background subshell, so it shares no variables with its
# siblings; every path it writes to is named after the row.
check_row() {
  local n=$1 dir=$2 spec=$3 cfg=$4 expect=$5 expect_states=$6 flags=$7
  local work="$META/$n" start=$SECONDS
  [[ "$flags" == "-" ]] && flags=""
  # shellcheck disable=SC2086  # flags is a deliberately word-split list
  run_tlc "$dir" "$work" "$work.out" -cp "$JAR" tlc2.TLC -workers 1 ${flags} \
          -metadir "$work/m" -config "$cfg.cfg" "$spec.tla"
  local code=$? got out states problem="" secs
  out=$(cat "$work.out")
  secs=$((SECONDS - start))
  if [[ -n "$TLC_KILL_REASON" ]]; then
    got="error($TLC_KILL_REASON)"
  elif [[ $code -eq 0 ]] && grep -q "No error has been found" <<<"$out"; then
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
    printf 'ok    %-58s %-34s %8s distinct %4ss\n' "$dir/$cfg" "$got" "$states" "$secs" >"$RESULTS/$n.report"
    echo ok >"$RESULTS/$n.status"
  else
    { printf 'FAIL  %-58s %s (%ss)\n' "$dir/$cfg" "$problem" "$secs"; tail -30 <<<"$out"; } >"$RESULTS/$n.report"
    echo fail >"$RESULTS/$n.status"
  fi
}

# Print the report of every finished row that is not printed yet. Only this
# (main) shell prints, so reports never interleave.
flush_reports() {
  local status n
  for status in "$RESULTS"/*.status; do
    [[ -e "$status" ]] || return 0
    n=$(basename "$status" .status)
    cat "$RESULTS/$n.report"
    [[ $(<"$status") == ok ]] || fail=1
    printed=$((printed + 1))
    mv "$status" "$RESULTS/$n.printed"
  done
}

# Rows that match the filter, biggest pinned state count first: the long
# configs start early, so they do not run alone at the end.
select_rows() {
  while IFS=$'\t' read -r dir spec cfg expect expect_states flags _note; do
    [[ -z "${dir}" || "${dir}" == \#* ]] && continue
    [[ -n "${FORMAL_CHECK_FILTER:-}" && "$dir/$cfg" != *"$FORMAL_CHECK_FILTER"* ]] && continue
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$dir" "$spec" "$cfg" "$expect" "$expect_states" "$flags"
  done < expected.tsv | sort -t $'\t' -k5,5nr -s
}

RESULTS="$META/results"
mkdir -p "$RESULTS"
fail=0
printed=0
total_start=$SECONDS
n=0
while IFS=$'\t' read -r dir spec cfg expect expect_states flags; do
  while (( $(jobs -rp | wc -l) >= TLC_JOBS )); do
    wait -n
    flush_reports
  done
  n=$((n + 1))
  check_row "$n" "$dir" "$spec" "$cfg" "$expect" "$expect_states" "$flags" &
done < <(select_rows)
wait
flush_reports
# A row whose job died before it wrote a status would otherwise vanish.
(( printed == n )) || { echo "FAIL  $n configs started, only $printed reported" >&2; fail=1; }
(( n > 0 )) || { echo "no configs matched" >&2; fail=1; }
echo "$n configs, $TLC_JOBS at a time, total $((SECONDS - total_start))s"
exit $fail
