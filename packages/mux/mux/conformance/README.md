# Offline conformance runner

Run `uv run pytest packages/mux -n 2`. The existing pytest-mux CI shard
collects this directory. No provider requests, SDK discovery or runtime wiring occur.

Register a driver-specific factory with `Registry.register(name, factory)`;
it returns a fresh `Adapter(ManagedAgents, store, ScriptedTransport)` per probe.
Call `await run(registry, name)` for C01–C18 results. Results carry `pass`,
`fail`, or `pending` and evidence. Pending prevents certification. Exceptions
are reported by class only so upstream exception messages cannot expose secrets.
Fixture-authored `ConformanceFailure` messages identify the failed check; explicit
`require` checks also run under `python -O`. Reference pending and pass IDs are
pinned in CI, so moving a probe from executable to pending fails the matrix.

An adapter seeds native responses in `arrange(Cxx)` and injects upstream faults
in `fault(name)`; it must not edit the driver's projected state or return a verdict.
C02 distinguishes disclosed expiry from unexpected continuity loss.
C02/C09 seed two binary artifacts, the first containing bytes 0–255, and C09
shares a channel vault. An interrupted download may resume with all exact bytes
or return a typed failure; truncated, duplicated or corrupted success is refused.
`Scenario.shared_resources` names the shared vault, and transport
`deleted_resources` logs actual provider deletions independently of receipts. C05 scripts child-first completion, stream disconnect,
overlapping saved/buffered items, paginated history and an unrecoverable gap.
The expected final item is `item` with text `done`; the one root tool result is
`call` with text `result`. Both identities and exact preserved content are checked.
C06 runs a tool at cancel acceptance, ends the stream without terminal evidence,
then supplies an observed interrupted root outcome. Both wait calls use fresh
bounded future deadlines. C08 deletes a required mount,
fails its replacement, then supplies successful reconciliation. Fixture source
contains the complete assertions and named faults.

The reference driver is an in-memory, partial, test-only oracle for the runner.
It is not a provider and its passes certify no backend. `IS_TEST_ORACLE = True`
marks the reference module; register it with a name containing `reference`. Unexercised ports refuse
explicitly. Nine executable probes cover C02/05/06/08/09/10/11/15/16. C10 checks driver
admission, extension version and cross-tenant rejection. The remaining
nine probes have explicit dependency reasons: N2 store, N4 host outcomes, N5 wakes,
N8 accounting, N10 selection.
They must be implemented against those seams before any C01–C18 certificate.

`runner.StateStore` is an opaque temporary protocol, with no invented store API.
Replace it with N2's protocol when merged; providing a store alone does not
activate the pending tests. Never interpret a skip as a pass or remove its reason.
