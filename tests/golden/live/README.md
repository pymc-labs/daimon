# Live golden scenario recipes

`scenarios.json` supplies N9's `tests/judge` harness with setup, exact user/host
turns, event-driven triggers, structural assertions, evidence requirements and
judge task definitions for all 33 offline goldens. It pins each original node ID
and canonical SHA-256. No existing golden is edited, re-recorded or used as a
precomputed live verdict.

This is offline preparation. **BOUND means a recipe exists**; it does not mean
an executable host callback exists or that the scenario ran. The loader has no
SDK, key loader, network, authentication or judge execution path. N9 owns the
live `Binding.start/run/cleanup/close` callbacks, actual observations, native
budget acknowledgement, audited margin and run admission. Every missing callback,
fixture or evidence checker must become a runtime BLOCKED result. The manifest
classifies 20 candidate live recipes and 13 source scenarios that cannot count as
live legacy turns under this unit.

```sh
uv run python tests/golden/live/bindings.py
uv run pytest -n 2 -q tests/golden/live/test_bindings.py
```

The loader validates closed typed records, one entry per source scenario,
canonical hashes, explicit blocker codes, consecutive turn numbers and distinct
`golden_<scenario>` task identity. Normalized live comparison must apply each
entry's assertions and the common assertions to newly observed facts. The
predicate strings are specifications for N9's registered checkers; they are
never evaluated as Python. A missing checker cannot pass. Structural checks
precede subscription judge grading. No model judge can override a structural
failure or supply a missing provider/tool observation.

The current judge's `load_tasks()` registry has only 12 tasks and rejects these
new IDs. The manifest's `judge_task` records deliberately match its `Task`
schema; N9 must add an explicit registry/loader. Reusing an unrelated existing
task ID would grade the wrong goal. N9 must report all 33 coverage rows including
typed BLOCKED rows, rather than presenting 20 executed candidates as 33 live
passes. For those blocked without any provider turn, record OFFLINE_CONTROL or
BLOCKED; do not reserve/spend a provider session solely to relabel a host check.

## Shared setup and observation contract

Use only disposable probe-owned Anthropic agents, environments, sessions and
files, and an isolated local host DB. The exact admitted model is
`claude-haiku-4-5-20251001`, per N9's corrected Haiku plan. The golden's Sonnet
model and synthetic fixed usage are not live inputs. Unsupported model/native
budget/margin enforcement refuses admission; no fallback. The run manifest's
budget policy is owned by N9 and applies before every session create, including
host cold creation, replacement, checkpoint and successor paths.

Run one provider session at a time. A two-session handoff/recovery must settle
and stop the old session before admitting its successor. Account for runtime,
tool charges and uncertain/failed attempts. The recipe limits count root turns,
including checkpoint/setup turns; actual model requests within one turn may be
multiple and remain subject to the native budget and verified in-flight bound.
The manifest never invents a maximum input size or a dollar margin.

Discord/Slack/DM recipes exercise actual host handlers with a real provider
client and **local platform effect recorders**. This is real provider execution
with HOST_SIMULATED platform coverage. It is not live Discord/Slack transport
coverage. Existing platform `_make_runtime` helpers install a fake provider
router and cannot be used unchanged. N9 must inject the admitted live client
into actual runtime/TurnDeps and observe the legacy `run_turn` invocation.

MCP fixtures require a probe-owned endpoint reachable by the provider, not an
unreachable host localhost or `daimon.invalid`. The Linear-shaped fixture has
only read-only `list_teams` and a local-log `create_issue`; it never contacts a
real Linear account. Native confirmation support must pause both calls, so the
read tests automatic allow without a card and the write tests a pending card.
The bounded slow fixture records actual start and has no mutable side effects.
Cancellation and ceiling use real observed stream/tool-start events and real
deadlines. Missing or missed preconditions are not normalized into a pass.

Use a created-resource registry with each create receipt and fresh run tag.
Normal host session creation can lazily provision a memory store; record and
clean those own resources too. Its default create request lacks N9's `qa_run`
tag. The host recipes therefore pre-create a truly tagged store through the
existing port and bind it through `stores.agent_memory_stores.insert_memory_store`
in the isolated DB. A missing provenance seam blocks the recipe; changing
captured receipts to invent a tag is forbidden. Similarly, multipart file
uploads need a real filename/create-receipt ownership seam; a JSON-only
resource registry cannot safely clean a full handoff's files without that seam.
Production re-hosting retains `daimon-handoff-<transfer_id>.tar.gz`. A registry
requiring every upload filename to start with `qa-<run_id>-` is insufficient
for this path: use reviewed proof from this run's transfer/source-session
lineage, or report the recipe BLOCKED. Renaming the production upload to pass
an ownership check would change the scenario being tested.
Stop active sessions, then clean only resources
created by the run in dependency-safe reverse order. Never enumerate/delete
the workspace, adopt prior handles or erase a cost receipt on cleanup failure.

Record N9-approved normalized events and redacted request metadata/audit facts.
Use in-memory checks to emit content-free facts for XML escaping, input
correspondence and confirmation ordering. Do not persist raw HTTP bodies or
free-form tool arguments. Evidence IDs must be unique and refer to actual
observations; fixture hashes/counts, model usage and outcome kinds stay typed.

## Outcome parity

Runtime IDs and observed timestamps vary; preserve same-session versus
successor identity, caller/tenant/account attribution and causal ordering.
Model wording, chunking, token count, latency and archive size may vary. Fixed
fixture bytes, hashes, required tool results, error kind, confirmation safety
and continuity level remain assertions. The shared canonical JSON files remain
read-only provenance and are never live expected byte tapes.

For locally billed cases, compare the actual set of provider model-call IDs to
usage/debit IDs, retain the requester and root-turn reason, and recompute exact
Decimal money from the admitted Haiku prices, token categories and markup.
Ledger `occurred_at` uses the actual provider `processed_at`. Do not assert the
golden's fixed Sonnet cost, token count or frozen epoch. Three continuation
root turns need three outcomes; they can produce more than three usage rows.

Cold Discord order remains status before session creation, and eyes after
initial-send acceptance. Slack eyes and status precede creation. Completion
feedback follows the observed terminal. Handoffs retain full/transcript/history
classification and loss disclosure; full transfer additionally compares actual
download/upload hashes and the successor's marker bytes.

## All 33 source scenarios

The manifest contains the complete setup/turn/assertion records; this table
shows scope and the reason a source cannot enter this legacy-turn matrix.

| Scenario | Recipe status | Live target or typed blocker |
| --- | --- | --- |
| plain_turn | BOUND | Fold actual streamed text; one end-turn terminal. |
| tool_use | BOUND | Real fixture read, native required action, one allow and no card. |
| approval_card | BOUND | Real fixture write held until authorized card approval. |
| approval_card_discord | BLOCKED: HOST_CONTROL_ONLY | Current/frozen local card-handler equivalence has no provider turn. |
| approval_card_slack | BLOCKED: HOST_CONTROL_ONLY | Same local equivalence limitation. |
| cancel_mid_stream | BOUND | Cancel after actual partial text/tool start; await stop acknowledgement. |
| reconnect | BLOCKED: PROVIDER_FAULT_INJECTION | No approved controller for the scripted connection drop and saved-item seam. |
| rate_limit | BLOCKED: PROVIDER_FAULT_INJECTION | No controlled native 429; quota exhaustion is not a probe. |
| mcp_degraded | BOUND | Real provider failure of a disposable unavailable MCP server, then successful reply with degradation. |
| ceiling | BOUND | Actual active stream exceeds host deadline; ceiling outcome and task cleanup. |
| billing_replay | BLOCKED: PROVIDER_FAULT_INJECTION | Replay-only model-call billing requires an injected stream omission. |
| cold_discord | BOUND | Actual host cold-create order, completion effects, usage and ledger dating. |
| cold_slack | BOUND | Same with Slack's actual feedback order. |
| dm_delivery | BOUND | Real private turns; two recorded DM replies and duplicate delivery suppressed. |
| plain_discord | BOUND | Actual billed host turn and caller attribution. |
| plain_slack | BOUND | Actual billed host turn and caller attribution. |
| blocked_balance | BLOCKED: HOST_ADMISSION_ONLY | Host refuses before any provider model turn. |
| blocked_cap | BLOCKED: HOST_ADMISSION_ONLY | Local admission control is not paid provider execution. |
| dead_session | BOUND | Delete only the created old session, verify real 404, disclose loss and bill successor. |
| handoff_full | BOUND | Real checkpoint bundle, upload/download hash parity and successor marker. |
| handoff_transcript | BOUND | Real archive with readable history; transcript-only successor. |
| handoff_history | BOUND | Real deleted event log; history-only successor with explicit loss. |
| wake_continuation | BOUND | One delivered private-input follow-up; truthful history and one idempotency key. |
| handoff_continuation | BOUND | Same responder binding; exactly one delivered task-handoff follow-up. |
| dm_turn | BOUND | Actual private session reuse/history, safe framing and idempotent billing. |
| sealed_channel | BOUND | Actual scheduled legacy turn carrying destination seal metadata. |
| scheduler_run | BOUND | Actual scheduled legacy turn uses channel environment over defaults. |
| timer | BLOCKED: HOST_DECISION_ONLY | Source checks decide_continuation/rendered seed but never dispatches a turn. |
| env_mount_failure | BLOCKED: PROVIDER_FAULT_INJECTION | No controlled native delete-success/add-failure operation. |
| mcp_start_turn | BLOCKED: NON_TURN_ENTRY_POINT | Direct session-send API bypasses legacy run_turn. |
| mcp_continue_turn | BLOCKED: NON_TURN_ENTRY_POINT | Direct send boundary; a second run_turn send would duplicate input. |
| mcp_cancel_turn | BLOCKED: NON_TURN_ENTRY_POINT | Direct interrupt adapter call, not the legacy turn pump. |
| cli_session_get | BLOCKED: NON_TURN_ENTRY_POINT | Native session inspection only; no model turn. |

Candidate bindings that require a native operation also require actual support:
archive must retain the readable history signature, deletion must really yield
404, MCP failure must name the server, and a confirmation must pause execution.
Unsupported capabilities are typed runtime BLOCKED. A supported operation that
violates its observed outcome assertion is FAIL. Additional live card, timer,
MCP-adapter or CLI probes can be useful separately named coverage; they cannot
replace these blocked source rows without an approved scope change.
