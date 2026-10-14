# Discord adapter

Gateway lease tests synchronize with PostgreSQL backend shutdown before a
successor attempts the advisory lock. The termination helper waits within a
bounded deadline for the old PID to disappear from activity and lock records;
the successor's lock attempt and fail-stop assertions remain single attempts.
The takeover test also gates the old holder's reconnect check until the successor
has acquired the lock, then calls the real check to prove the old holder stops.

The output-delivery module exposes `deliver_provider_output` for verified neutral
provider bytes. The caller must validate the admitted tenant, provider session
and authoritative root before supplying a `DeliverableFile`. This edge does
not select outputs or fetch provider content; host/event wiring is separate.

Uploads reuse answer attachment edits and standalone `discord.File` fallback.
Existing answer attachments are preserved. The guild size limit and the current
writer protection still apply.

The `DaimonBot` constructor accepts explicit optional `http_client` and UTC
`clock` dependencies for in-process tests. Ordinary construction retains the
existing HTTP client and clock. The explicit `daimon.testing.discord_ingress`
helper drives original gateway messages through real ingress/admission and
observes title/queue/HTTP evidence; see `tests/GATEWAY-INGRESS.md` for its owned
runtime contract and the separate native completion requirement.

This title/admission slice rejects injected provider runtimes, backends and
stores at the observation boundary. Captured Anthropic fixture completion is
separate from independently pinned alternate-provider completion.


`DiscordConcurrentDriver` extends the opt-in testing ingress with real queue,
reaction, binding/lease and card checkpoints for concurrent busy-thread and
owned tenant-cap probes. See [the ingress contract](tests/GATEWAY-INGRESS.md).
Native gates and independent scenario verdicts remain runner-owned.

Ingress receipts also expose actual HTTP message-fetch replies and discord.py's
parsed message references. Channel bursts retain each authored identity and
separate production admission; chunk replies bind to an actually emitted SDK
message. See the source-pinned burst/chunk controls in the ingress contract.

The posted-card renderer accepts an optional persistent button factory. Fleet
buttons use the registered `FleetControl` dynamic item for both live clicks and
restart recovery. Their IDs carry the run, worker, version and action; these
are lookup and compare-and-set identities, never authority. Ordinary private
form cards retain their existing serialized components.

`DiscordFleetCard` reads canonical fleet rows through the shared
`FleetOrchestrator.read_snapshot` operation. Its host-injected parent loader
reloads current permissions on each control click; the card cannot supply root
facts. The loaded subject must match the clicker; the shared authorization
owner decides whether that subject may control the orchestrator's run.
Stop and Resume use the same core operation, scoped versions and stable
operation keys as the tools. An accepted write retires the old controls and
draws the current rows; it does not claim native termination or completed cost.
Complete status rows stay in ordinal order while they fit within Discord's
4,000-character total text limit, including the headline and budget footer.
Larger fleets end with one `+N more` line counting the omitted rows. Admission
and run-level controls cover the whole fleet. Within the component limit, individual
controls favor the newest active workers; remaining active rows are marked
status only. One Stop all and Resume all pair uses the run's own version and
the same shared operation, so admission does not depend on display capacity.

The runtime's optional `fleet_card_factory` is consulted only when fleet
settings are explicitly enabled. Missing factories leave ordinary turns
unchanged. The existing render loop updates the fleet card, and its normal
final-answer path waits for authoritative collection and required reviews.
Pending or refused results never become an answer from worker excerpts.
Intermediate sealed answers stay private on every fleet lifecycle path,
including terminal failure; only the collected final summary is delivered.
Native execution, durable atomic control, root loading and usage accounting
remain their existing owners' ports; absent ports refuse rather than succeed.

The host factory accepts an optional `run_id` keyword for recovered-card
lookup and must rebuild the current root binding from authenticated tenant and
thread facts. A run handle cannot manufacture that binding. A card is bound at
turn preparation, so a run spawned mid-turn needs the owner's subsequent wake
turn to render it. Waiting collection ends that root delivery without an answer;
the same wake owner must arrange its later reviewed summary. The card does not
claim that those currently absent owner bindings execute.

`build_runtime` installs the concrete Discord factory when fleet is enabled
and the host supplies the shared `FleetOrchestrator` and authenticated root
resolver. Enabled construction without either dependency refuses before SDK
or database construction. Disabled construction never calls or installs them.
The resolver receives the genuine turn-card intent, destination, current actor
and optional captured run ID. It runs during rendering, after preparation;
only a proven absence can return no active fleet. Unknown, missing or foreign
authority refuses. The consumer does not construct root grants or select a
latest session; the shared preparation/origin owner must supply that binding.

Recovered controls look up the real post in the actual tenant/thread and use
its recorded intent and original attribution. The factory verifies that
attribution against the loaded root before shared control authorization.
Edits use `DiscordPostTransport`, including webhook recovery, and acknowledged
replacement message IDs carry the original post attribution in the existing
post store. Live refresh and collected-card edits adopt the returned message,
so subsequent edits target the actual replacement. Capture is metadata only;
there is no production QA recorder or new card table.

When the trusted fleet resolver proves there is no active fleet, the installed
factory preserves the complete ordinary lifecycle delivery, including sealed
pre-tool replies and terminal failures. Missing or foreign authority stays
closed, and active fleets expose only the reviewed collected summary.
Replacement capture passes canonical stored attribution to the shared
`record_turn_post` owner; its timeout, transaction and failure logging also
apply to recovered controls.

With a shared fleet service and explicit trusted root-grant loader installed,
`build_runtime` binds the card factory to `read_frozen_root` automatically.
Both real turn callers forward their genuine card intent. The existing Postgres
state store is shared by turn execution and root lookup. Recovered controls
resolve the original requester's stored account and retain the canonical host
scope, then reload the current clicker's subject through the existing role
readers. Missing or stale authority refuses before a card is sent. Disabled
fleet settings install no reader, store or grant callback. Native control,
worker dispatch and wake lifetime remain the shared service's responsibility.
