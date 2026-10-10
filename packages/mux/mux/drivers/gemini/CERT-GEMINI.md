# CERT-G — Gemini live evidence, 2026-10-10

The lead's D1 live threshold is met: the narrow smoke and **three C-cases with
real Gemini provider I/O passed**. All used `gemini-3.8-flash`; no retries or
fallbacks occurred. This is not an all-eighteen conformance certificate.

The evidence is in [certification/2026-10-10](certification/2026-10-10/).
This artifact-only PR is based on merged integration (#650 harness, #648 exact
spend). Replay uses the unchanged recorder from that base; recorded aliases are
ordinary strings and require no unmerged alias or live-scenario code.

| Evidence | Classification | Verified actual USD | Held USD |
|---|---|---:|---:|
| Smoke rerun | Live provider PASS; exact saved `GEMINI_SMOKE_OK`, one completed root | 0.002799 | 0 |
| C10 | Live provider PASS: real interaction, capability/scope refusal before further writes | 0.00285525 | 0 |
| C15 | Live provider PASS: real interaction, unsupported migration preserves binding | 0.00288525 | 0 |
| C16 | Live provider PASS: real interaction, raw-handle/extension bypass refused | 0.0030915 | 0 |
| C01–C18 first matrix envelope | Four contract-only passes, fourteen typed PENDING; zero provider I/O | 0 | 0 |
| **Verified total for included runs** | | **0.01163100** | **0** |

The earlier failed smoke is separate and is not included as successful evidence.
Its canonical receipt still has actual=null and held **0.147456**. Its known
captured-token tariff is 0.00281775, but final completeness/reconciliation was
not established by that run's saved artifacts. Therefore total unresolved held
funds for this unit remain **0.147456**, separately from verified actual spend.
No ledger receipt was removed or rewritten to hide the failure. N9/lead own its
reconciliation; the failure NOTE is in the sprint inbox at 07:32Z/07:37Z.

Each native C-case created one actual bounded interaction, observed its completed
reply and usage, then ran the **unchanged** shared C10/C15/C16 assertions against
that native-bound session. Each owned interaction's provider DELETE succeeded;
request metadata and sanitized HTTP-response usage sidecars record the cleanup.
No account-wide resource discovery/deletion occurred. The provider governs the
inline environment lifetime; no environment-deletion receipt is invented.

Recorded worker heads: smoke rerun
`185c3dba0b4d96d19dd58ed9c8366a2cf1d881c8`; real C-cases
`bfa5d0c50a37a9e37e6a47c2569878cc23ff2962`. These identify the historical run code,
not dependencies of this PR. The smoke's old report abbreviates its head;
this document supplies the full SHA. Native C-case code included the reviewed
status/overrun corrections before the paid runs. Its later source PRs are
reviewed separately from these immutable normalized artifacts.

| Case | Current result and origin |
|---|---|
| C01 | PENDING: multi-human host admission/batching hook absent |
| C02 | PENDING: expiry/unexpected workspace-loss live scenario absent |
| C03 | PENDING: acknowledged/lost-acceptance fault and restart hook absent |
| C04 | PENDING: ambiguous accepted POST reconciliation unavailable |
| C05 | PENDING: deterministic stream-gap/child-first saved-item live fixture absent |
| C06 | PENDING: pre/post-cancel barrier and root alias live fixture absent |
| C07 | PENDING: controlled usage revisions/corrections and overlap fixture absent; smoke is not C07 PASS |
| C08 | PENDING: next-turn tool/mount update port absent |
| C09 | PENDING: provider vault API/session-delete receipt unavailable |
| C10 | PASS: real provider setup/completion/cleanup, unchanged contract assertions |
| C11 | PENDING: interaction-time skill deployment scenario bridge absent |
| C12 | PENDING: host ledger/outbox recovery hook absent |
| C13 | PASS, **contract-only**: local state binding CAS/restart, no provider I/O |
| C14 | PENDING: existing/new-thread selection hook absent |
| C15 | PASS: real provider setup/completion/cleanup, unchanged contract assertions |
| C16 | PASS: real provider setup/completion/cleanup, unchanged contract assertions |
| C17 | PENDING: host wake-generation/lease-fencing hook absent |
| C18 | PENDING: host termination outcome persistence hook absent |

The original matrix envelope retains its honest pre-native classifications.
The later C10/C15/C16 receipts supersede those rows' **evidence origin**, without
rewriting the earlier files. Overall: three live-provider PASS, one contract-only
PASS, fourteen typed PENDING. Full default capabilities (F2), authenticated MCP,
binary skills and host turns are not certified by these runs.

Every HTTP response saves whitelisted prompt/candidates/cached/thought counters
and a closed native status. Interaction usage is cumulative: repeated GETs are
not added together. Neutral output includes candidates plus thoughts exactly
once. Each receipt links its usage observation hash, dated rates, measured token
charge, actual USD, held USD and accounting status in the canonical N9 ledger
`sprint/lanes/N9-qa/spend.md`. The approved Gemini line is $30 with a $24 stop.

Reviewed prices per million tokens on 2026-10-10: input $0.75, output including
thoughts $3.75, cached input $0.075, effective through 2026-12-31. Inline preview
infrastructure is unbilled under the reviewed official policy; no paid grounding
or explicit cache creation was configured. [Official Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

Tapes contain normalized contract events, credential-safe request metadata and
short identity aliases, never keys, native bodies or raw SDK objects. Receipts
and usage sidecars contain only financial metadata and whitelisted counters.
The offline replay test consumes every tape, checks native create/completion/
delete evidence and cumulative usage against the dated receipts, verifies typed
PENDING and zero-I/O matrix origins, and rejects changed saved output. It reads
no provider key and permits no network fallback.
