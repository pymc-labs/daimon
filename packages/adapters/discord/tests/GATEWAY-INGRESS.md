# Explicit offline Discord ingress

`daimon.testing.discord_ingress.DiscordIngress` feeds original gateway
`MESSAGE_CREATE` payloads into discord.py's real parser, dispatcher and scheduled
`DaimonBot.on_message` callback. It uses normal admission, production queue state,
thread creation and title generation. No caller-provided XML, pre-created
`qa-thread`, synthetic turn-to-thread map, rewritten message ID or automatic
completion certificate exists.

Construct it with an owned `DiscordRuntime`, explicit tenant/account `Scope`,
fresh `IngressHTTP` and the runtime's actual isolated `ScriptedTransport`.
`open(guild=..., channel=..., bot_user=...)` validates actual DB ownership and
configured backend before initializing the owned gateway cache. Primary and all
routed database binds use the shared ownership guard, including the registered
isolated-CI exception. Contributor tests skip plain local shared databases.

`deliver(payload)` preserves source event order and message identity and returns
without waiting for a turn to finish. Distinct equal-text messages remain
separate production events and can queue behind an active thread callback.
`wait()` awaits the actual scheduled callbacks. `receipt()` reads actual queue
state, thread-create requests/replies, complete captured post operations, native
title request bytes, real naming-meter rows and HTTP history reads. A changed
backend or unbound SDK transport is refused. `close()` cancels unfinished owned
callbacks; it does not report cancellation as successful native completion.

`IngressHTTP` accepts only the owned parent and threads actually created through
Discord's SDK edge. Thread IDs follow Discord's originating-message identity.
History returns the original gateway payloads through actual SDK history reads;
production context builders decide what reaches the native send. The controlled
Anthropic fixture verifies the original preceding context and trigger ID in the
actual native events request, then completes through the real host driver. This
is a host-wiring control, not an independently pinned catalog completion tape.

The fixture's optional `hold_first_post=True` pauses after the first serialized
card is accepted, before native session allocation. This exposes real admission,
queue claims and title evidence without sending into an unbound provider. With
no hold, only the captured Anthropic SDK path is authorized by this slice. The runner
must suppress its own external Sentry transport. The fake HTTP fixture has no
fallback to a live Discord connection.

`DaimonBot` accepts optional `http_client` and UTC `clock` dependencies. Omission
keeps the existing HTTP client and `datetime.now(UTC)` at ordinary admission and
turn-deadline calculation. The injected clock does not virtualize core timeouts,
scheduler jobs or title timeout waits. The capture HTTP's monotonic clock remains
separate and explicitly injected.

For the first C7 slice, six A/O/G probes use the frozen I6 and NEW9 steps. I6
retains the preceding context event but the existing title path sees only the
bare trigger: it skips naming and opens `Chat with qa-agent`. That static name
passes I6's literal forbidden-pattern/length assertion, without proving a
context-derived model title. NEW9 executes the real shared Haiku title call,
meters it to the original requester/message, and opens under the SDK response.
A/O/G share this existing ancillary title path; no provider-native root is
inferred from it. The controlled title response passes NEW9's title predicate.

All six whole catalog cells remain **PENDING**: the paused probes have no
source-pinned native root/terminal and independent full-oracle join. Initial card
visibility or title predicate success cannot certify NEW9's complete transient
text/count assertions or source `wait_done`. N3/N9 own that join; this ingress
module provides the callable edge. Frozen YAML is unchanged.

Mutations remove production ingress, substitute a valid-looking title without
its SDK call/meter, and corrupt the generated title before real thread creation.
Their observations lose the required path or fail the literal title predicate.
The equal-text control reads actual production `_pending` messages and queue
claims, with independent pooled DB connections for concurrent callbacks.

Concurrent DB reads in production may reverse the pending queue order relative
to gateway dispatch order. The hook records both original dispatch and actual
production pending IDs without sorting or repairing either. This first slice
preserves event delivery and identities; it does not certify queue-order policy.


The explicit ingress guard validates both the current outer runtime sessionmaker
and the actual admission `turn_deps.sessionmaker`, including every routed bind,
before bot allocation, open, dispatch and receipt reads. Outer, nested Anthropic
and optional Messages dependencies must share the actual captured isolated SDK
client and its owned MockTransport, without mounted fallback transports. The
real scheduled `on_message` callback repeats this check before product admission;
replacing a frozen bundle after delivery does not authorize its new dependencies.
Negative controls reach every boundary, trap foreign connections before connect,
and retain a separate isolated SDK trace to detect uncaptured requests. A nested
routed-bind control also proves refusal before bot allocation. The supplied SDK
uses the shared construction guard, including the N8 no-retry and TLS/proxy
follow-up; no local environment policy is duplicated.

The shared `daimon.testing.discord_runtime_capture` validator fails closed on
opaque injected provider dependencies: `turn_runtime_factory` must be None,
`turn_runtimes` an ordinary empty dict, and `backend`, `backend_session_ref` and
`state_store` all None. The default store uses the validated sessionmaker.
The title/admission slice never authorizes native deployment construction;
`release_first_post` permits only the captured Anthropic path in this slice.
Alternate-provider completion requires a separate explicit offline capture
binding. A pause does not authorize an otherwise unsafe runtime. Negatives use
unheld construction and a released first-post gate at scheduled callback entry,
including the genuine default factory, an uncaptured SDK backend, and a real
foreign PostgresStateStore with a connection trap.

The fixture uses product-default thread naming and a neutral unused settings
DSN; actual connections use its owned factory. The shared pytest11
`db_preflight` plugin runs before DB engine/session/cleanup fixtures and uses
shared owned-engine admission, including registered isolated CI. Its cache key
tracks each requesting fixture's resolved `db_schema`. It skips only a plain
local contributor DB outside exact CI. Child pytest tests install connection
traps before the actual ingress fixture graph, proving foreign refusal and
contributor skip happen without connection. No local autouse guard remains.
This hook certifies neither a live provider
nor a whole cell; A/O/G I6/NEW9 title facets retain their prior observations and
whole-cell PENDING verdicts.


### Concurrent offline Discord ingress

`daimon.testing.discord_ingress.DiscordConcurrentDriver` adds `checkpoint`,
`wait_for` and `deliver_while_busy` to the opt-in ingress boundary. Checkpoints
read the actual production pending queue, turn-slot counts, original gateway
IDs, timestamped SDK reaction history and posted HTTP payloads. Legacy session
rows and provider-native binding slots/leases are read through scoped, read-only
queries; a running OpenAI/Gemini turn is never inferred from an Anthropic row.
The driver refuses concurrent delivery before an actual active host binding.

The runner owns the native runtime, wire and clocks. Hold the actual first root
at its running stream gate, deliver the second original event without waiting
for the first callback, observe the real queued message/reaction, then release
and capture the real continuation. For the tenant-cap case, use the signed
`AdminBindings.execute` turn-cap/default source binding against the owned tenant,
hold two actual roots, observe the third actual card/queue ticket, then release
one root while the other remains active. Clock advancement is an interleaving
control; it does not alone certify an elapsed native interval or a timeout.

The controlled native Anthropic fixture exercises these adapter paths and actual
session reuse/release through the installed isolated SDK. OpenAI/Gemini cases
are PENDING until an explicit owned native capture binding can pass the same
dependency guard; their fixtures skip before ingress allocation. The Anthropic
records are not independent QA recipe/oracle pins, and do not prove execution
of the requested bash sleep. All whole-cell verdicts remain PENDING. The
NEW6 probe retains a full 15-second real waiting-card history under both explicit
`discord.tenant_cap_waiting_card=False` and `=True` settings. The flag-off
path still accepts its ordinary card and fails the queue-copy predicate. The
flag-on initial mention renders queue wording on that same card while its real
ticket waits at the owned tenant cap, then edits the same ID to ordinary progress
before the third real root starts. Only the flag-on rendering facet may read
**PASS under opt-in profile**; the missing native sleep recipe/oracle join still
keeps whole cells PENDING. OpenAI/Gemini bindings remain explicitly PENDING.
A mutation removes the product waiting-card hook and the real ingress witness
fails the queue-copy assertion. Running and global-only queued tickets, foreign
tenant context, released tickets and default-off payloads retain ordinary copy.
The adapter observes only the original mention admission ticket; drained core
successor turns keep their existing wording. No new card, notice, timer, provider
request, queue claim/release or admission policy is introduced. N4's product
profile owner adds the default-False/opt-in-True seventh switch; a setting receipt
alone supplies no native completion or whole-cell evidence.
Production enqueue/cap mutations must break the corresponding queue witness;
a cosmetic hourglass or a caller-owned gate flag alone is not admission proof.
No production policy, reducer, provider request, routing or default changes.

The concurrent fixture retains the first slice's explicit settings: init-only
placeholder authentication and unused configuration DSN, owned tenant credit,
`thread_open_notice_after_s=0` (product default 3), mux path and channel backend
selection enabled, and external Sentry disabled. Naming uses the product-default
`ThreadNamingSettings()`. Only the owned tenant's cap is changed through the real
admin binding and restored to default on exit; ordinary runtime construction
does not import or instantiate this driver.

### Channel bursts, authored twins and chunk replies

`parsed_replies` records reference/target/channel/author IDs read from the real
scheduled discord.py `Message`, before invoking the unchanged production
callback. `message_fetches` records the owned HTTP request target and original
response JSON. The fake HTTP bot-author payload includes the required nullable
`avatar` field so the SDK constructs an actual User in a resolved reference.
These observations certify neither native completion nor a source assertion.

NEW3 preserves one source parent channel and three distinct authored mentions.
Three separate real threads, sessions and roots remain concurrently gated before
release; production admission is never replaced by completed sequential turns.
The injected source clock records offsets 0/1/2 seconds. Real native gates and
bounded waits remain independent; these logical offsets do not certify live
network latency. Serializing production channel callbacks loses the real queue
and persisted-binding witness even though all three events were delivered.

NEW4 retains both original equal-text messages with distinct IDs at source
offsets 0/1. The actual path creates two threads and two accepted answers. Both
frozen max-one predicates FAIL as expected by the catalog; there is no repeated
ID, probe dedupe or invented billing readback.

NEW10 sends the original no-tools prompt, then a controlled authored native
120-line response through the installed isolated Anthropic SDK and real chunk
renderer. Accepted overflow messages have individually balanced fences, a
captured gap check and the summary on the last chunk. Every non-fence body line
is retained in native order, including all later overflow, and compared against
the actual native stream record. The runner fetches actual
chunk 2 through `discord.Thread.fetch_message`, uses its SDK reference and exact
HTTP reply in the next original gateway event, and observes the real parser's
resolved target/author plus same-session native continuation and CHUNK-OK post.
Overflow-drop and parser-reference-drop mutations lose the witnesses. Dropping
just later overflow is detected even when two balanced chunks and a last-chunk
footer still satisfy the catalog's minimum shape checks. A missing parsed
reference can still answer an explicit mention but cannot claim reference binding.
An actual-SDK missing-avatar regression detects malformed fake author payloads.

Anthropic NEW3/NEW10 adapter facets PASS; their whole cells remain PENDING for an
independent native recipe/oracle join. Anthropic NEW4 is an executed expected
FAIL on its actual threads/answers predicates. All three O/G native cases remain
PENDING and skip before ingress allocation; the shared runtime validator does
not admit opaque alternate-provider dependencies. Shared ownership admission and
the authorized temporary fixture preflight remain unchanged. Source YAML,
product requests, routing, queue policy and ordinary defaults are unchanged.


The observer forwards main's `startup_replay` and `resume_admitted` arguments
unchanged; ordinary gateway delivery uses their false defaults. The flag-on
initial waiting-card renderer keeps the actual cap passed to that mention's
synchronous admission alongside its returned ticket. This replaces the cap field
removed from main's ticket without changing core current-cap readmission.
