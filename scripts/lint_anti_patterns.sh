#!/usr/bin/env bash
# Grep-based test anti-pattern lint.
#
# Enforces the bans documented in guideline:testing on SDK-fake construction:
#   T1  MagicMock(id=...) returned as an SDK shape. Callers treat the mock's
#       .id as a real prefixed-string id; UUID coercion of that mock then
#       passes tests and crashes in production. Use the approved fake
#       (build_fake_anthropic from _ma_fake) or construct the real Beta-model
#       inline.
#   T2  model_construct(...) anywhere in the test tree — banned (skips
#       Pydantic validation; produces silently-invalid objects).
#   T3  AsyncMock(...) attached to client.beta.* — banned. Always go
#       transport-level via httpx.MockTransport.
#   T4  client.beta.{agents,environments}.list in production source — banned
#       (cross-tenant leak). Allowlisted: ma_index.py, ma.py (filtered-list homes).
#   T5  .beta.agents.create( outside the approved creation chokepoints — banned.
#       Every create must guarantee the base agent_toolset (via dump_agent_spec
#       or merge_default_agent_toolset); an agent without it 400s at session
#       create as soon as skills are attached ("skills require the read tool").
#       Allowlisted files apply one of the two; a new create site must do the
#       same before being added here.
#   T6  get_earliest_tenant anywhere — banned (oldest-tenant path retired)
#       (oldest-tenant path is retired).
#   T7  client.beta.skills.list in production source — banned outside ma_index.py
#       (the chokepoint). All skills listing must go through list_skills_strict or
#       list_skills_lenient which enforce truncation semantics.
#   T8  find_skills?_by_display_title( outside sanctioned canonical-title callers
#       — banned. Every allowlisted module builds its lookup title via
#       tenant_scoped_display_title. New callers must be vetted and added here.
#
# Each rule emits a list of file:line matches. Exits non-zero with the number
# of failing rules if any pattern is found outside an allow-list.
#
# Run locally with: bash scripts/lint_anti_patterns.sh
#

set -euo pipefail

# Scope: test trees only. Adjust as the implementation phase formalizes.
SEARCH_PATHS=(
  "packages/adapters/discord/tests"
  "packages/adapters/cli/tests"
  "packages/adapters/mcp/tests"
  "packages/adapters/slack/tests"
  "packages/core/tests"
)

# Scope: production source (T4 and future prod-only rules).
PROD_SEARCH_PATHS=(
  "packages/core/daimon"
  "packages/adapters/cli/daimon"
  "packages/adapters/discord/daimon"
  "packages/adapters/mcp/daimon"
  "packages/adapters/scheduler/daimon"
)

# Files where each rule's match is acceptable (e.g. the fixture file itself
# that exports the approved alternative). Empty by default; the
# implementation phase can grow this allow-list with intent.
ALLOWLIST_T1=()  # MagicMock(id=...) for SDK shapes
ALLOWLIST_T2=()  # model_construct
ALLOWLIST_T3=()  # AsyncMock on client.beta.*
ALLOWLIST_T4=("ma_index.py" "ma.py")  # the legitimate filtered-list homes
# Approved agent-creation chokepoints: each guarantees the base agent_toolset
# via dump_agent_spec or merge_default_agent_toolset.
ALLOWLIST_T5=(
  "core/defaults/reconcile_agents.py"
  "mcp/tools/agents.py"
  "cli/commands/agents.py"
  "discord/agent_setup/write.py"
  # preflight probe agents are archived immediately — never host sessions/skills.
  "core/defaults/preflight.py"
)
# get_earliest_tenant is fully retired — the oldest-tenant
# path no longer exists anywhere. Zero allowlist: any occurrence fails.
ALLOWLIST_T6=()
# T7: skills.list chokepoint — only ma_index.py may call it directly.
# All other code must use list_skills_strict (write contexts) or list_skills_lenient
# (read contexts) from ma_index, which enforce truncation semantics.
ALLOWLIST_T7=("defaults/ma_index.py")
# T8: canonical-title lookup chokepoint — only the approved modules that build
# their lookup title via tenant_scoped_display_title may call find_skills?_by_display_title.
ALLOWLIST_T8=(
  "defaults/ma_index.py"
  "defaults/skills.py"
  "defaults/reconcile_skills.py"
  "skill_sync/orchestrator.py"
  "skills/sync.py"
  "mcp/tools/skills.py"
  "cli/commands/skills.py"
)

DIVIDER="------------------------------------------------------------------"
PASS=0
FAIL=0

run_rule() {
  local name="$1"; shift
  local description="$1"; shift
  local pattern="$1"; shift
  local -n allow=$1; shift

  echo
  echo "[$name] $description"
  echo "$DIVIDER"

  # Build grep --exclude-dir for allowlisted files. grep doesn't accept
  # file-level allowlists directly; we collect hits then filter.
  local hits
  hits=$(grep -rnE "$pattern" "${SEARCH_PATHS[@]}" 2>/dev/null || true)
  if [ -z "$hits" ]; then
    echo "  no matches"
    PASS=$((PASS + 1))
    return
  fi
  local filtered="$hits"
  for allowed in "${allow[@]:-}"; do
    [ -z "$allowed" ] && continue
    filtered=$(echo "$filtered" | grep -vF "$allowed" || true)
  done
  if [ -z "$filtered" ]; then
    echo "  all matches were on the allow-list — clean"
    PASS=$((PASS + 1))
    return
  fi
  echo "$filtered" | sed 's/^/  /'
  local count
  count=$(echo "$filtered" | wc -l | tr -d ' ')
  echo
  echo "  $count match(es) — FAIL"
  FAIL=$((FAIL + 1))
}

run_rule_prod() {
  local name="$1"; shift
  local description="$1"; shift
  local pattern="$1"; shift
  local -n allow=$1; shift

  echo
  echo "[$name] $description"
  echo "$DIVIDER"

  local hits
  hits=$(grep -rnE "$pattern" "${PROD_SEARCH_PATHS[@]}" 2>/dev/null || true)
  if [ -z "$hits" ]; then
    echo "  no matches"
    PASS=$((PASS + 1))
    return
  fi
  local filtered="$hits"
  for allowed in "${allow[@]:-}"; do
    [ -z "$allowed" ] && continue
    filtered=$(echo "$filtered" | grep -vF "$allowed" || true)
  done
  if [ -z "$filtered" ]; then
    echo "  all matches were on the allow-list — clean"
    PASS=$((PASS + 1))
    return
  fi
  echo "$filtered" | sed 's/^/  /'
  local count
  count=$(echo "$filtered" | wc -l | tr -d ' ')
  echo
  echo "  $count match(es) — FAIL"
  FAIL=$((FAIL + 1))
}

echo "===================================================================="
echo " lint-anti-patterns"
echo "===================================================================="

# T1: MagicMock(id=...) for SDK return shapes.
# Matches both `MagicMock(id=uuid.uuid4())` and `MagicMock(id="agent_…")`.
run_rule "T1" \
  "Banned: MagicMock(id=...) used as SDK-shape return value" \
  '\bMagicMock\([^)]*\bid\s*=' \
  ALLOWLIST_T1

# T2: model_construct anywhere in tests.
run_rule "T2" \
  "Banned: model_construct(...) (skips Pydantic validation)" \
  '\.model_construct\(' \
  ALLOWLIST_T2

# T3: AsyncMock on client.beta.* methods.
run_rule "T3" \
  "Banned: AsyncMock attached to client.beta.* methods" \
  'client\.beta\.[a-zA-Z_.]+\s*=\s*AsyncMock' \
  ALLOWLIST_T3

# T4: unfiltered agents/environments .list() in production source (cross-tenant leak).
# skills.list is banned separately by T6 — the chokepoint (ma_index.py).
run_rule_prod "T4" \
  "Banned: unfiltered client.beta.{agents,environments}.list in production (cross-tenant leak)" \
  'client\.beta\.(agents|environments)\.list' \
  ALLOWLIST_T4

# T5: agents.create outside approved chokepoints (base-toolset guarantee).
run_rule_prod "T5" \
  "Banned: .beta.agents.create( outside approved chokepoints (must guarantee base agent_toolset)" \
  '\.beta\.agents\.create\(' \
  ALLOWLIST_T5

# T6: get_earliest_tenant anywhere (oldest-tenant path fully retired).
run_rule_prod "T6" \
  "Banned: get_earliest_tenant (oldest-tenant path is retired; resolve via derive_tenant_uuid)" \
  'get_earliest_tenant' \
  ALLOWLIST_T6

# T7: direct client.beta.skills.list in production source outside ma_index.py.
# All skills listing must go through list_skills_strict / list_skills_lenient
# in ma_index.py which enforce truncation semantics.
run_rule_prod "T7" \
  "Banned: client.beta.skills.list outside ma_index.py (use list_skills_strict / list_skills_lenient)" \
  'client\.beta\.skills\.list' \
  ALLOWLIST_T7

# T8: find_skills?_by_display_title( outside sanctioned callers.
# Each allowlisted module builds its lookup title via tenant_scoped_display_title
# (mechanical enforcement). New callers must be vetted here.
run_rule_prod "T8" \
  "Banned: find_skills?_by_display_title( outside sanctioned canonical-title callers" \
  'find_skills?_by_display_title\(' \
  ALLOWLIST_T8


echo "$DIVIDER"
echo " Summary: $PASS rule(s) clean, $FAIL rule(s) failing"
echo "$DIVIDER"

exit "$FAIL"
