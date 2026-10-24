Gemini's driver-owned conformance seams are explicitly imported from
`mux.testing.drivers.gemini.conformance_hooks`. They are not installed by the host
factory and do not select a provider or discover credentials.

`recover_send` consumes the original scoped session, immutable input, operation
key and injected StateStore. It validates the stored principal, request digest
and optional binding slot. Sent or unknown operations return a typed
`SendRecoveryEvidence` with an `outcome_unknown` receipt, no input IDs and basis
`native_lookup_unavailable`. Gemini exposes no lookup by the host's operation
key. Text similarity, current root and session status cannot establish that
association. A complete original durable acknowledgement can retain its queued
receipt; acceptance is not successful processing. Missing identity, changed
input/authority, and invalid acknowledgements refuse. Pending or failed intent
requires an explicit send decision; this hook never issues a POST or alters the
operation. An ordinary owned session read may GET its already-known interaction.

The actual SDK/MockTransport proof injects death before and after send-claim and
acknowledgement commits. Each boundary uses its own session. Native effects
survive independently of driver storage, and recovery never blindly resends.
Lost acceptance retains unknown delivery across driver/store reconstruction and
blocks a different operation key. The shared C04 fixture remains typed
ADAPTER_DEPENDENCY: its fenced multi-operation host envelope is not connected.
This proof does not bypass Gemini's occupied/uncertain-session admission.

`prepare_inline_skill` creates one immutable local skill pin, binds it to the
supplied Gemini agent specification, creates the supplied environment, and
accepts the host's SessionSpec callback. The callback must preserve that exact
agent revision and environment. It returns `InlineSkillDeployment` containing
local resources and the prepared session. Its catalogue operations are not
native uploads. Deployment occurs on the subsequent interaction POST. A caller
must independently capture that request body to prove the original file bytes,
inline target paths, repository configuration, explicit tools and MCP binding.
No native skill ID, uploaded-resource receipt or executed repository checkout is
invented.

The SDK proof checks all original UTF-8 bundle bytes, including companion files,
a valid SKILL.md, the pinned mount after restart/new-version publication, native
repository intent and public remote MCP configuration. EOF leaves both custom
function actions and unhandled native actions occupied; unsupported native
execution refuses. A custom result continuation keeps its root, environment and
explicit tool list. Changed digest/foreign skill identity refuses before SDK I/O.
The shared C11 fixture remains typed ADAPTER_DEPENDENCY because its upstream
`skills.create` upload oracle does not describe Gemini's interaction-time local
catalogue deployment. N9 owns any provider-aware fixture composition; N5 owns
host capability preparation, N4 fenced turn authority, and N7 platform adapters.

C06 cancellation and C07 usage already pass the existing offline matrix. Native
live envelopes remain separate. C08 next-turn tool/mount mutation is unsupported
by the current port, so configuration changes refuse rather than silently update
an occupied session. C09's local binary/snapshot paths do not supply a native
vault or session-delete receipt. Their typed PENDING boundaries remain intact.
No fixture, profile, default backend, live certificate or golden is changed.

The API shapes were checked against the retained official 2026-10-10 references:
`source-G-agent.txt:184-218` (inline skills), `source-G-env.txt:265-280`
(repository sources), and `source-G-runtime.txt:587-613` (remote MCP). Sources:
https://ai.google.dev/gemini-api/docs/custom-agents,
https://ai.google.dev/gemini-api/docs/agent-environment,
https://ai.google.dev/gemini-api/docs/antigravity-agent.
The tests use only MockTransport with a literal offline credential. No paid
calls, live key reads, ledger writes or historical hold releases occur.

Both SDK proof fixtures use `mux.testing.isolated_sdk.build_gemini_client`
with explicit offline auth and owned sync/async MockTransport clients. Provider
environment overrides are refused before SDK allocation; both clients close
on fixture exit. The helper is test-only and is not imported by the driver.
