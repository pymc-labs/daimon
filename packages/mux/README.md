# mux

The provider-neutral managed-agent contract: types, port protocols, errors,
profiles and admission, the `StateStore` protocol and its in-memory test
store (`mux.state`), with one driver package per provider under
`mux/drivers/`. Daimon wraps its existing Anthropic client with resource
ports while retaining host policy, authorization, locking and client lifetime.
Turn and event ports remain independently injectable in the driver factory.
Resource drivers include vault credentials, files and session administration;
credential specifications carry opaque references resolved for one request.

The import name is the top-level `mux`, not `daimon.mux`, so it can be split
into its own repository without a rename. `mux` never imports `daimon`, and
only `mux.drivers.<provider>` imports that provider's SDK; import-linter
contracts in the root `pyproject.toml` enforce both.

Install a driver's SDK with its extra: `daimon-mux[anthropic]`,
`daimon-mux[openai]` or `daimon-mux[gemini]`.

The full description is [docs/mux.md](../../docs/mux.md). Tests:
`uv run pytest packages/mux`.

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
