# Teams host conformance

Import `daimon.testing.adapters.teams.conformance` explicitly from an offline
host envelope. The application does not import the module. Use a dedicated
idle app, isolated database, authored inputs and an offline transport.

`multi_human_evidence(MultiHumanEnvelope)` reports actual orchestration facts,
never a verdict. The first native-root callback per thread receives an
`AdmissionBarrier(started, release)`: its normal host execution signals
`started` after real admission/root acceptance, and its upstream completion
waits for `release`. The probe queues follow-ups through normal orchestration,
then releases that root and lets the normal drain finish. The callback receives
exact platform inputs and must perform ordinary admission/binding/execution.
Save the original bound execution method before probing when forwarding to it;
forward every captured parameter unchanged. The scheduling barrier itself is
not a native acceptance receipt. A callback returning before queuing is refused
even if the surrounding orchestration tail remains active.

The envelope requires two distinct threads under one declared channel, unique
messages and two human callers. It refuses busy or invalid inputs before
callback side effects. Exception, timeout and cancellation await probe tasks,
restore the previous handler and release its own state/tickets. Normal
orchestration retains cap reads, routing, reaction/continuation tails and
cleanup. Concurrent controls use pooled connections rather than a shared
single-connection transactional fixture.

Facts contain the platform, received/queued identities, claimed ownership,
actual callback batches, callback overlap, and final queue/processing state.
`callback_returned` is false when a callback raises, including an error caught
by the adapter's normal boundary. A true value only says the callback returned,
not that a native root completed successfully. Shared C01 needs independent
real host/provider workspace, config, principal, native-root and persisted
charge facts. Provider owners compose these with the raw platform mapping;
N9 owns shared fixture checks/activation. Pending declarations are unchanged,
and synthetic callback controls do not certify a live or complete C01 run.

`MultiHumanEnvelope(app, tenant_id, entra_tenant_id, channel_id, threads, run,
timeout_s=30)` uses `ThreadInputs(first, queued)` of verified `TeamsInbound`.
`run(MentionTurn, barrier | None)` receives the actual inbound and tenant.
The probe temporarily captures `_run_turn_guarded`, preserving normal setup
routing and continuation dispatch. Teams replies to each caller's last message
and retains earlier `composed_ids`, every `message_id` and ordered files.
The callback receives full original file objects; returned facts expose only
file kind/name and omit download URLs.

Production orchestration alone owns thread claims, queue settlement and final
release; the probe does not discard processing state or clear pending inputs.
Callback completion/overlap are observed at the production-selected callback
boundary. In-process mutations bypass the real busy-thread branch or collapse
cross-author grouping, and the same independent platform fixture rejects both.
