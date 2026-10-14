# Discord host conformance

Import `daimon.testing.adapters.discord.conformance` explicitly in an offline host
envelope. The application does not import it. Use a dedicated idle `DaimonBot`;
the probe temporarily captures its mention handler and restores it on success,
callback failure, timeout or cancellation. It never attaches to a live gateway.

The conformance tests use the adapter conftest's shared schema, engine and
cleanup fixtures. Their callback sessionmaker still opens separate pooled
connections for committed admission rows. Re-exporting those session fixtures
inside the test module gives pytest another schema dependency and can tear down
an engine while another fixture still owns its transaction.

`MultiHumanEnvelope` supplies the bot, tenant UUID, guild ID, channel ID, at
least two `ThreadInputs(first, queued)` under that channel, and a `run` callback.
The bot needs its normal gateway identity and a ready tenant in the host DB;
the tenant UUID must match normal guild derivation. Messages must mention the
bot, have unique IDs and at least two human authors. Each thread has
queued follow-ups. `timeout_s` is a finite positive overall deadline.

`run(MentionTurn, AdmissionBarrier | None)` receives the exact actual message,
guild/tenant and content/attachment overrides passed by Discord's drain. The
first call per thread gets a barrier. Its host transport signals `started`
after normal admission and actual root acceptance; it waits for `release`
before completing that root. Every first and follow-up message enters through
`bot.on_message`, including normal protection/liveness reads, busy-thread
routing and tenant-slot admission. The probe never claims or enqueues a thread
itself. After follow-up ingress returns it observes the bot's actual pending
IDs and claimed state, then releases the barrier. The bot owns draining and
its `finally` release, including resuming deferred continuations.
Save the original bound mention handler before the probe if forwarding to it;
forward all captured parameters unchanged. The callback must perform normal
admission/binding/execution; it must not hand-pin admissions or return verdicts.
The barrier itself is a scheduling control, not an acceptance receipt.

The returned mapping contains `platform`, `threads`, `batches`,
`peak_callbacks_by_thread`, `queue_empty_after` and `processing_empty_after`.
Thread facts record received and queued message IDs, claimed ownership and
whether the first callback finished before queuing. Batch facts record the
thread, first message, caller, actual composed content, override flag and
attachment IDs. Peak callback overlap is observed at the production-selected
mention handler boundary; callback completion is recorded at that same boundary.
Callback overlap is not a native active-root count. Callback failures are also
raised by the explicit probe even when normal ingress renders them as errors.

The shared C01 oracle also needs independent actual host/provider facts:
same configuration and credential principal in a channel, distinct workspaces
across its two threads, one active root per thread, and caller-attributed
charges. In opt-in shared mode, callers share their thread workspace; they
never share the other thread's workspace. Native journal, prepared bindings
and actual persisted usage/ledger facts must prove these separately. A queue
control with synthetic callbacks is not full C01 or live certification. Missing
provider choreography and shared-runner activation remain typed PENDING.

The controls use provisioned isolated PostgreSQL rows, actual ingress,
queue/drain/reaction paths and parsed Discord SDK messages, threads, users and
attachments. Process-only mutations disable the production busy-thread branch
or merge callers during production batching; the same independent platform
oracle rejects both changed mappings. Production release also resumes deferred
continuations, including on cancellation. They also reject invalid/busy inputs
before callback side effects and check restoration/cleanup. No provider calls,
SDK fields, usage rows, golden files or default behavior are fabricated.

The pooled admission controls own a module-local worker schema and pool through
the shared guarded DB fixtures. They do not reuse another test module's pooled
transactions. Failure controls first observe entry into the real callback, then
expire a real `asyncio.Timeout` or cancel the probe explicitly; callback failure
keeps its original exception. A separate watchdog bounds the test, and every
exit joins its tasks before checking handler restoration, queues and tenant slots.
