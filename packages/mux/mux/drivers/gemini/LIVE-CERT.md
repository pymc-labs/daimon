The Gemini probe harness defaults to MockTransport. It does not read a real key,
write the shared spend ledger, enable a host backend or make a provider call.

From the repository root, create an empty private output directory, then run:

```bash
uv run python -m mux.drivers.gemini.live_cert --model gemini-3.5-flash-lite --output /absolute/empty/output
```

This exercises the pinned SDK's real serialization with a synthetic response.
The synthetic mock ledger uses the same allowed model ID, with test-only rates.
It saves a smoke tape, eight offline fixture tapes, and a report with all eighteen
C-matrix results. Each matrix row declares `offline_driver` as its evidence
origin. Ten typed PENDING scenarios remain visible. The narrow smoke is tagged
C07 for budgeting/recording; it does not prove live usage corrections and never
returns a full live certificate.

After lead authorization, the live command is:

```bash
uv run python -m mux.drivers.gemini.live_cert --live --model gemini-3.5-flash-lite --budget /absolute/reviewed-budget.json --output /absolute/empty/output
```

Live preparation and the direct SDK entry require exactly `gemini-3.5-flash-lite`
before key loading or client creation, even if another model has configured prices.
No Pro, larger Flash tier, alias or fallback is allowed. The official offline runtime
model table (`source-G-runtime.txt:735-749`) lists Flash-Lite as the tier optimized
for cost-sensitive tasks. It supplies no numeric rates; add only that exact ID when
its rates have been verified. The provider's implicit 3.8 Flash default is never used.

Only this mode reads `~/.config/daimon-nc/gemini.env`. The file must be owned by
the running user, mode 0600, with one `GEMINI_API_KEY=...` assignment. No ambient
key, Vertex routing or environment endpoint override is used. The API endpoint
is the Gemini API. Never place a key in command arguments or a report.

`live-budget.example.json` pins the existing N9 shared ledger and the $30 Gemini
allocation. Live preparation and the SDK entry reject any other ledger path
before key loading or client creation; private ledgers are allowed only for
MockTransport runs. The N9 guard refuses reservations exceeding the 80% threshold ($24),
persists reservations before I/O and retains uncertain/failed spend. The model
price table is intentionally empty: the lead must add reviewed per-million-token
input/cached-input/cache-write-input/output rates. Missing prices refuse before
provider I/O; test prices are synthetic and confined to the mock ledger. Do not
initialize, replace or reset the existing shared ledger.

The smoke permits one create POST, uses zero SDK retries, and requests the
provider's documented `agent_config.max_total_tokens=4096`. It reserves 32768
input and 32768 output tokens, polls usage for overruns, and stops at 120 seconds.
Timeout/failure requests bounded cancellation only when a native interaction ID
is known. A lost acceptance response is never retried or declared cancelled.
Unknown usage retains the financial reservation; overruns block future admission.
The smoke requires the exact `GEMINI_SMOKE_OK` saved reply and one observed root
completion. Credential-safe metadata and normalized events are recorded through
N9's recorder; request body values and SDK objects are not exported.

The official local runtime snapshot (`source-G-runtime.txt:1314-1319`) describes
max_total_tokens as best effort and excludes cached tokens. The financial
reservation is conservative, not a promised hard provider spending bound. A live
run establishes only the smoke's evidence; the separate matrix is still offline.
Live fault/host adapters and all PENDING probes must be resolved before a full
live certificate can be issued. No key watcher or automatic live invocation is
installed, and importing this module performs no I/O.
