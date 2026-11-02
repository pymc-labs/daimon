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

The host factory accepts an optional `run_id` keyword for recovered-card
lookup and must rebuild the current root binding from authenticated tenant and
thread facts. A run handle cannot manufacture that binding. A card is bound at
turn preparation, so a run spawned mid-turn needs the owner's subsequent wake
turn to render it. Waiting collection ends that root delivery without an answer;
the same wake owner must arrange its later reviewed summary. The card does not
claim that those currently absent owner bindings execute.

Recovered controls look up the real post in the actual tenant/thread and use
its recorded intent and original attribution. The factory verifies that
attribution against the loaded root before shared control authorization.
Edits use `DiscordPostTransport`, including webhook recovery, and acknowledged
replacement message IDs carry the original post attribution in the existing
post store. Live refresh and collected-card edits adopt the returned message,
so subsequent edits target the actual replacement. Capture is metadata only;
there is no production QA recorder or new card table.
