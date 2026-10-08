# Dreams: memory tidy-up design

Status: design only, nothing built. Checked against the Managed Agents docs and
this repo on 2026-10-08.

## What a dream is

A dream is a Managed Agents job that reads one memory store and up to 100
session transcripts and writes a reorganised store: duplicates merged, stale or
contradicted entries replaced, new insights added. It runs for minutes to a few
hours and is billed at standard token rates for the model it runs on. It is a
research preview: access is granted per organisation, and its request shape may
change without a deprecation period.

Request (`POST /v1/dreams`, beta `dreaming-2026-04-21`):

```json
{
  "inputs": [
    {"type": "memory_store", "memory_store_id": "memstore_..."},
    {"type": "sessions", "session_ids": ["sesn_...", "..."]}
  ],
  "model": "claude-sonnet-5-5",
  "instructions": "optional, up to 4,096 characters",
  "output_behavior": {"type": "create_new"}
}
```

`output_behavior` is `create_new` (the default: a new store that starts as a
copy of the input) or `update_existing` (rewrites the input store in place). A
dream has `status` `pending`, `running`, then `completed`, `failed` or
`canceled`, a `usage` block with token totals, and a `session_id` for the
session it ran in. It has no `metadata` field.

## Why Daimon wants it

Each agent has one memory store per tenant (`agent_memory_store`,
`memory_resource.py`), shared by every channel the agent works in. Nothing
consolidates it. The only guard against duplicates is one sentence in the
memory instructions ("update existing files instead of appending duplicates").
A store holds at most 10,000 memories of up to 100 kB each, and a busy agent
keeps adding to it. A periodic dream would keep the store small and current
without Daimon writing its own consolidator.

## What production memory looks like (2026-10-08)

Read-only measurement of the hosted deployment:

- 35 live stores across 13 tenants. Only 17 hold anything: 81 files, 573 kB,
  480 writes. 399 of the writes rewrote an existing file. Agents keep a few
  notebook files per store and edit them in place.
- Exact repetition is low: 153 of 5,885 lines (2.6%), almost all in one store.
  A dream's value here is not merging duplicates.
- The problems are layered corrections and logs:
  - About 60 "CORRECTION", "REFUTED" or "current truth" markers sit in 19
    files, each with the wrong claim still above the fix.
  - In the largest store, 88% of one 41 kB file is repeated "no new findings"
    run logs.
  - Another file in that store shrank itself ten times in 119 writes and grew
    back each time, despite a note saying "do not re-expand". Consolidating
    within a session does not hold.
- Value is concentrated: three stores hold 58% of the bytes and most of the
  layered corrections. Seven stores are small and stale, and would need
  pruning by age rather than merging.
- Some contradictions span stores: the same fact is right in one agent's store
  and wrong in another's. A dream works on one store at a time and cannot
  reconcile them.
- Agents have saved skill and code files into memory (a `SKILL.md`, a Python
  helper, a skills index). A dream must not rewrite those.
- In the 7 days to 2026-10-08, the busiest store had 38 sessions that could
  write to it; most had under 10. In a sample of recent transcripts the median
  was about 8k tokens and the 90th percentile about 250k. So a 20-session
  dream on Sonnet 5.5 would cost roughly $1 to $5. This is an estimate, not a
  measured run.

User-side evidence from the Discord archive is thin, and most memory
complaints concern preferences saved but not applied, not the MA stores.
There are about 15 explicit "remember this" requests in 60 days, a few cases
of a team re-teaching the same convention, and a few stale facts that drove
wrong advice. No one reported memory leaking between channels.

## Design

### Inputs

- **Store**: `get_memory_store_id(tenant_id, agent_id)`. One dream per
  (tenant, agent) store.
- **Sessions**: `thread_sessions` rows for that tenant and agent, updated since
  the store's last dream, newest first, at most 100.
- **Only sessions that could already write to this store.** Before a session
  goes in, read it from MA and keep it only if its mounted `memory_store`
  resource is this store with `access: read_write`. That one rule excludes:
  - direct messages under the tenant's `dm_memory_read_only` policy,
  - routine runs (always mounted read-only, `headless_runner.py:332`),
  - sealed channels and inherited sealed work (`turn/admission.py:562-564`,
    `749-770`),
  - sessions on another store, for example before a channel copy.

  As a second check, also drop any session stamped `daimon_private_dm` or
  `daimon_sealed` in its MA metadata. A dream reads whole transcripts, and its
  `instructions` can only steer synthesis, not filter content. So the input
  list is the only place to keep private material out of a store that other
  channels read.
- **Skip the run** when fewer than a minimum number of new writable sessions
  qualify (a setting, default 5).
- **Model**: `claude-sonnet-5-5`, the cheapest supported model ($2 input / $10
  output per MTok). Haiku is not supported.

### Output: no store swap

Daimon never swaps an agent onto a new store while it has live sessions.

**The swap hazard.** With `create_new`, adopting the result means pointing
`agent_memory_store.memory_store_id` at the new store. `memory_store_id` and
memory access are session identity fields (`session_compat.py:162-163`), and a
store can only be attached when a session is created. So on its next turn every
live thread session of that agent gets `ReplaceSession`. Each replacement
archives the old session and spends a billed checkpoint turn on it to carry the
files across (`session_preparation.py:437,791`, ledger reason
`checkpoint_debit`). A failed checkpoint loses the sandbox disk. On top of that,
anything a live session writes to the old store while the dream runs is lost at
the swap.

Daimon therefore uses the two output modes like this:

1. **Review phase: `create_new`, never adopted.** The job compares the output
   store with the input (memories added, removed, changed) and keeps the result
   for operator review, then archives the output store. No session sees it. This
   measures cost and quality with no effect on users.
2. **Live phase: `update_existing`, in a quiet window.** The dream rewrites the
   agent's own store, so no binding changes and no session is replaced. It runs
   only when the agent has no running session. Before it starts, the job records
   the store's current memory versions, so an operator can roll back with the
   memory versions API, which keeps versions for 30 days. A second
   `update_existing` on the same store returns 409, which the job treats as
   already running. This needs a credential that can write memory stores
   (otherwise 403). The docs do not say what happens when a live session writes
   during the rewrite. The quiet-window rule avoids that case rather than relying
   on it.

The live phase starts only after the review phase has shown that outputs are
trusted.

**Files that are not notes.** The review diff flags any change to a file that
is not a markdown note (skills, code, anything under `/skills/`). In the live
phase the job puts those files back to their pre-dream version.

**Instructions.** The dream's `instructions` ask for one current statement per
fact, with superseded claims and repeated run logs removed. They can only
steer, so the review phase checks that it worked.

**Trial stores.** The review phase runs on the four stores with the most
layered corrections, not on every store.

### Trigger

- Review phase: an operator command next to `daimon memory`, for one tenant and
  agent, which prints the diff.
- Live phase: a `_sweep_memory_dreams` job in the scheduler, following the
  existing `_sweep_*` pattern (single instance by advisory lock, errors logged
  and contained). It needs a persisted last-run row per store, because the
  scheduler's in-process watermarks reset on restart. It runs at most once a
  week per store and is off unless a tenant setting turns it on.

### Billing

A dream runs in its own MA session, and Daimon cannot tag it: there is no
metadata on dream create. `usage_sweep.py:124-125` skips any session without
`daimon_tenant`, silently, so today a dream would cost money and appear nowhere.

The job records the cost itself when the dream ends. It prices `dream.usage` at
the dream model's rates, the same way thread naming and the classifier are
recorded (`record_thread_naming_usage`, `record_classifier_usage`), under a new
ledger reason, `dream_debit`. That reason joins `SPEND_LEDGER_REASONS` so promo
credit covers it. The store belongs to one tenant, so attribution is clear; it
is not tied to a channel or a person.

Not documented: whether the $0.08 per running session-hour also applies to a
dream's session. The job reads the session's `active_seconds` and logs it, so
the first invoices answer the question.

Cost scales with the number and length of the input transcripts. The review
phase records actual `usage` per run before any tenant pays for one.

### Deletion and privacy

- Account purge hard-deletes that account's sessions (`ma.py:419`,
  `purge.py:450,583`). Deleting an input session fails a running dream
  (`input_session_unavailable`). The job retries once without the missing
  sessions.
- Purge deliberately leaves memory stores alone (`purge.py:73-76`), so a dream
  does not change what purge removes. In the review phase, though, `create_new`
  makes extra stores that hold synthesised content. The job archives each one
  after review and never keeps one past the review.

### SDK

The installed `anthropic` 0.117.0 already has `client.beta.dreams`. It lacks
the typed `output_behavior` field (added in 0.122.0). The review phase uses the
default and does not need the field. The live phase can send it through
`extra_body` until the 1.x upgrade lands. The upgrade does not block Dreams.

### What users see

Nothing in the review phase. In the live phase, the `/memory` view could show
when the store was last tidied. That text follows the UX bar for every surface:
words first, reviewed, then built.

## Blockers

| Blocker | Answer |
| --- | --- |
| Research-preview access | Not granted. On 2026-10-08 both deployment keys got HTTP 404 on `GET /v1/dreams?limit=1` (the same as a made-up path), while memory stores and sessions returned 200. Access is requested through the Managed Agents form at `https://claude.com/form/claude-managed-agents`. |
| Who pays | Open decision. Proposed: the operator absorbs the review phase; in the live phase the store's tenant is debited at model rates times the usual markup, as `dream_debit`. |
| SDK upgrade for `output_behavior` | Not a blocker. The review phase uses the default; the live phase can pass the field with `extra_body`. |

## Recommendation

Go for the review phase as soon as access is granted: four stores,
`create_new`, outputs reviewed and archived, operator pays, nothing changes for
users. Decide on the live phase after reviewing those diffs and their measured
cost. Fix the routine prompts that write run logs into memory now, whatever
happens with dreams.

Decisions needed from the operator:

1. Request research-preview access for the organisation.
2. Who pays in the live phase: the operator, or the store's tenant as
   `dream_debit`.
3. Whether an agent that serves several clients from one store may be dreamed.

## What a dream will not fix

- **Run logs at the source.** A routine that writes "no new findings" every
  run will refill the store. Its prompt needs fixing either way.
- **Contradictions between stores.** These need a cross-agent pass or shared
  facts, which is out of scope here.
- **One store per agent across several clients.** An agent that answers in
  several client channels keeps all of them in one store. That is how memory
  works today, with or without dreams. But a dream reads every writable
  transcript into one synthesis, so it could blend those contexts further. The
  live phase waits for a decision on whether such agents get a dream at all.

## Not decided here

- How many sessions and how often per store, after the review phase shows
  cost per run.
- Whether to use Opus 5.5 ($4 / $20) instead of Sonnet 5.5 if review outputs
  are weak.
