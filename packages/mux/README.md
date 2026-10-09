# mux

The provider-neutral managed-agent contract: types, port protocols, errors,
profiles and admission, the `StateStore` protocol and its in-memory test
store (`mux.state`), with one driver package per provider under
`mux/drivers/`. Daimon wraps its existing Anthropic client with resource
ports while retaining host policy, authorization, locking and client lifetime.
Turn and event ports remain independently injectable in the driver factory.
Resource drivers include vault credentials, files and session administration;
credential specifications carry opaque references resolved for one request.
Credential errors and SDK request logs redact raw, quote-escaped and nested
repr/JSON secret values. Request codecs preserve the original JSON key order.
Session resource lists resolve all repository token references for the same
request. Native environment creation retains explicit null descriptions through
its closed environment config schema.

The import name is the top-level `mux`, not `daimon.mux`, so it can be split
into its own repository without a rename. `mux` never imports `daimon`, and
only `mux.drivers.<provider>` imports that provider's SDK; import-linter
contracts in the root `pyproject.toml` enforce both.

Install a driver's SDK with its extra: `daimon-mux[anthropic]`,
`daimon-mux[openai]` or `daimon-mux[gemini]`.

The full description is [docs/mux.md](../../docs/mux.md). Tests:
`uv run pytest packages/mux`.

Conformance adapters can declare individual fixtures PENDING with typed capability,
live-key or dependency reasons. Declarations remain visible and never certify.

The Anthropic skills version extension also supports workspace-key downloads by
native version ID, omitting the skills beta header on that request. The ordinary
pinned-version port and generic recovery download keep their existing SDK requests. Both paths enforce the same
host-provided scope and skill grant before I/O.

### Anthropic turn event translation

`AnthropicManagedAgents` registers an unwired `AnthropicEvents` port over the
host's existing SDK client; an explicitly injected Events implementation still
wins. No host turn path, database writes, client retries or settings change.
Sessions lifecycle remains a separately injected port.

The Events port checks host resource authorization before sending, listing,
streaming or requesting cancellation. `open_stream` returns after the HTTP
connection opens, independently of its first event, so hosts can preserve
connect-before-send order even on idle sessions. Records and previews normalize to owned
`Event` values; previews use separate identities and never imply completion.
Required actions carry the referenced call and retain native thread routing.
Tool results with absent or null content retain their call pairing and native
record while exposing empty neutral content. Listed pages follow the SDK's
continuation rule; terminal pages expose no cursor. Malformed records raise an
owned provider error on both the list and stream ports.
Unknown native records survive as `native.*`, while subagent status records use
`agent.thread.*`. A cancel receipt acknowledges a request, not a stop. A lost
POST acknowledgment returns `outcome_unknown` without a port-level resend.

Anthropic has no root-turn IDs, resumable SSE cursor or atomic turn precondition.
`EventNormalizer` can receive the host's root identity; a chronological history
walk otherwise anchors turns on user input or the first running record. Pending
actions whose source call is outside that walk remain native. SSE cursors, turn
preconditions, reconciliation, stop waiting and native input extensions are
explicitly unsupported in this first unwired implementation. The cancellation
and host bridge units provide their policies separately.

`usage.observation_from_event` converts a model-request end span to revision 1 of an
owned `UsageObservation`, retaining the native meter. Input tokens include cache
reads and writes; a missing bucket remains unknown rather than becoming zero.
`usage.observed` events reference the observation ID and revision. No accounting
write or provider call occurs during normalization.

Native provenance can retain an opaque JSON `record` for the temporary host
compatibility edge, including native error retry timestamps and permission
details. It never contains an SDK object and survives JSON and pickle round
trips. Neutral reducers consume the normalized payload.
Session lifecycle ports now create, retrieve and list native sessions, with archive
delegated to session administration. Closed `anthropic.session_create@1` and
`anthropic.session_resource_create@1` configs preserve omitted and explicit empty
fields, agent overrides and mount ordering. Session specs carry extensions and
records carry an opaque optional native snapshot for the temporary host codec.
Anthropic model spans convert to `UsageObservation` in the usage driver,
retaining event IDs, timestamps and the untouched native meter. Neutral input
counts include cache stages; the host projects them into its existing billing
and telemetry columns. Temporary host compatibility entrypoints accept existing
SDK callers while turn and adapter ports migrate.

Gemini's explicitly constructed `GeminiManagedAgents` driver runs the non-core
`gemini.inline_reuse` profile. The host injects transactional driver storage,
a StateStore and a private transport; importing or constructing it makes no
provider requests. Agents and environment definitions are local inline
configuration records. Turns respecify that configuration and reuse both
`previous_interaction_id` and the returned environment ID. Expired or missing
continuity raises `ContinuityLost` instead of provisioning a fresh workspace.
The scripted transport and memory storage are offline test tools, not a
production persistence implementation or a live certification.

### OpenAI driver core

`mux.drivers.openai.OpenAIDriver` is an explicitly constructed, unwired Agents API
driver for persistent hosted workspaces and opt-in conversation-only sessions.
It uses the pinned OpenAI SDK's public HTTP primitives, normalizes root outcomes
and previews, reconciles paginated saved work while buffering the stream, and
reports nullable revisioned turn usage. Host authorization, recovery checkpoints
and revision allocation are injected. Unsupported native preconditions and
unfinished resource ports refuse explicitly. See the driver's README for verified
endpoints, state ownership and offline validation. Existing defaults stay Anthropic.
The persistent profile is temporarily non-core until skills and artifacts land;
its missing mandatory capabilities are surfaced by admission. Vault and multiagent
support are unsupported, and complete workspace export/import is unknown.

The native `anthropic.session_walk@1` extension exposes `SessionWalk.walk(scope)`
as an async iterator of neutral sessions with opaque native snapshots. It sends
no list filters and follows the SDK paginator exactly. Workspace-wide billing
sweeps pass a justified platform scope; tenant walks retain grants and tag checks.
