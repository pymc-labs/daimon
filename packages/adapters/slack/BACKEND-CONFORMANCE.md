# Backend-selection conformance callback

Explicitly import `daimon.testing.adapters.slack.backend_conformance` in an offline host
and call `backend_selection_turn(BackendTurnEnvelope(...))`. Supply a dedicated
idle application, isolated database, scripted provider/platform transports, the
native input and independently declared tenant/channel/thread/caller. The hook
drives `SlackApp._handle_app_mention`. Teams identity/authentication verification precedes this boundary.

The callback forwards normal admission and binding calls unchanged and returns
their actual `PreparedTurn` after normal execution. A normally rendered
`AdmissionDenied` is re-raised as that same exception for the core collector.
Core may return an admission resolved to the retained revision; the callback
checks its caller account without requiring object identity or the current
revision. Missing/multiple calls, wrong identity, busy apps, overlapping probes and invalid
timeouts fail closed. Observers are restored on every exit; unrelated tasks and
runtimes forward unchanged. The running application never imports this module.

Compose first/existing/new-thread callbacks with the core owner's
`BackendSelectionEnvelope`, using CURRENT channel config every time. Revisions
and provider bindings must be read independently from StateStore. Do not supply
a manually pinned old admission, fabricate a native reference or hide an
original-runtime selection refusal. These callbacks alone do not certify C14
or a live host. Foreign provider/runtime configuration remains owner supplied.

The regressions use real Postgres admission/mapping rows and the pinned SDK
through MockTransport, with unmodified turn execution. They verify session
reuse, distinct new-thread and caller bindings, current-config refusal without
provider writes, and a mutation of the real adapter `reuse_existing` argument
that breaks the independent binding oracle. No keys or live requests are used.

SDK fixture construction uses `daimon.testing.isolated_sdk.build_async_anthropic`
with the original synthetic key, endpoint, retry count and timeout. Ambient SDK
configuration is refused before construction and on SDK copies. Boundary controls
exercise the real fixture, copy/with_options and actual scripted HTTP requests.
