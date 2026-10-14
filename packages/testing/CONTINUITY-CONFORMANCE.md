# Continuity conformance hooks

These helpers are imported explicitly by QA envelopes. Applications and the
Anthropic path do not import them. They perform no SDK discovery or paid calls,
return facts rather than verdicts, and never clear an adapter's PENDING entry.
N11/N12 own native arrangements; N9 owns the shared C-case oracle.

`daimon.testing.continuity_conformance` supplies:

- `ProbeClock` and `LossProbe`: start before the actual workspace expiry, then
  exercise the scoped Sessions read at expiry or after an upstream loss. Use a
  fresh probe/clock for each branch. The provider owner must bind the clock to
  its driver/storage boundary and supply an async loss callback. Expiry requires
  explicit expiry evidence; unexpected loss must occur before expiry and carry
  different evidence. Both require the original binding and zero new upstream
  mutations. Facts are labelled `controlled_host_fault`. A host clock cannot
  invent provider expiry: the current OpenAI clock-only control refuses it.
- `collect_snapshot_bytes`: reads the exact seeded 256 bytes before and after
  two port turns and checks the two new saved inputs. A required workspace
  bootstrap can already exist; its journal entries remain intact. The caller
  owns artifact publication and independent resource/session references.
- `HistoryExpectations` and `collect_history_evidence`: declare native root,
  message-item and tool-call identities before reading output. The real Events
  and Sessions ports must preserve running occupancy after child-first/EOF,
  complete saved `done`/`result` text, unique paginated records, an authoritative
  completed root, released occupancy and an explicit history gap. A supplied
  restart callback constructs a fresh driver over retained storage. Exact saved
  records must survive; subsequent reconciliation may advance its cursor and
  append new gap/checkpoint records. Offline memory storage proves driver
  reconstruction only; live envelopes must supply their durable host storage.

`daimon.testing.accounting_conformance.HostAccountingHook` is an async callable
for `transport.host_accounting_evidence`. Supply an owned lane `AsyncEngine`:
the local PostgreSQL database must match `daimon_test_nc_<lane>` (worker,
evaluator and reviewer suffixes are accepted), with no URL query overrides.
Construction rejects shared `daimon_test`, unrelated test names and remote
hosts before acquiring a connection. The sole exception is `daimon_test` when
both `GITHUB_ACTIONS` and `CI` are exactly `true`, and `build_test_engine` has
registered this engine with the current process's `test_[wf]<pid>_<8hex>`
isolated schema. Arbitrary execution-option labels do not establish provenance.
The registry does not change engine, connection or schema behavior. Supply a
scoped pre-provisioned binding/session/model, aware observation time, and fresh
namespace. It writes controlled synthetic observations with test pricing, never
native usage or prices. Queried facts use the existing C12 snapshot format:
complete legacy usage row identity plus exact Decimal ledger keys and amounts.

The production neutral accounting applier proves historical replay, signed
100→120→110 corrections, channel spend, rollback between projection and ledger,
and pending-outbox recovery/replay. A listener on one connection raises before
the ledger INSERT, reads the uncommitted 120 projection, and is removed in a
`finally` block. Fresh store/session objects read rollback and recovery facts.
No global writer monkeypatch, trigger, schema change or migration is involved.

The legacy shared C12 arrangement requires turn/session aggregates covering the
same leaf. The current neutral ingest refuses overlapping/mixed grains rather
than accepting that arrangement. The hook reports actual
`aggregate_rejections`, empty persisted aggregate rows and unchanged host rows;
it does not manufacture accepted coverage. The unchanged shared C12 refuses
those incomplete facts. Keep C12 PENDING until the accounting and oracle owners
resolve the supported coverage arrangement. These controls do not certify live
provider billing, hosted expiry, delegation or an entire C-case matrix.

Construct the envelope with
`Adapter(..., pending={**existing_pending, "C12": hook.pending_reason})` before
dispatch; its pending declarations are immutable after construction. This typed
`ADAPTER_DEPENDENCY` declaration prevents the shared runner from presenting
supplemental recovery facts as a complete C12 certificate. The same typed
reason accompanies collected facts; it does not override any fixture verdict.

Offline OpenAI read probes use the shared isolated SDK factory with an explicit
transport, unchanged fixture auth/URLs and zero retries. Ambient provider
configuration is refused before allocation. Local `daimon_test` remains refused
by the accounting hook; its real-database tests skip that contributor setup
outside GitHub Actions CI. Use an owned lane DB for those local hook tests.
