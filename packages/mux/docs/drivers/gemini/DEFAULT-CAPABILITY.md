Gemini's supplementary F1 adapter is explicitly selected through
`mux.testing.drivers.gemini.default_capability.adapter`. Pass it to the shared
`run_default_capability` entry, or as the fresh factory for
`replay_default_capability`. It constructs only an offline driver and in-memory
resources; no key discovery, SDK client or live invocation is installed.

F1 currently returns typed `ADAPTER_DEPENDENCY` before provisioning. The full
authored default includes the complete `pymc-artifact-style` skill, with binary
fonts/images beyond native inline limits. The driver supports an explicit
`skill_bundle_publisher` implementing `SkillBundlePublisher`: it receives every
original file byte and returns a `GCSBundleDeployment` for a versioned directory.
The receipt binds tenant, account, provider account, version and the exact sorted
path/SHA-256 manifest. It is validated before acknowledgement and again when a
pin is mounted; storage persists it across restart. Each directory mounts at
`.agents/skills/<skill-id>`, retaining every relative path and binary asset.
Create/publish replays do not publish twice; old pins retain their old directory.

The official [environment documentation](https://ai.google.dev/gemini-api/docs/agent-environment)
(`source-G-env.txt:447-490,1449-1450`) defines raw-text inline sources at 1 MiB
per file and 2 MiB total, and GCS directory sources up to 2 GB. The driver retains
a 64 MiB bundle bound for host storage. Combined inline mounts respect native
limits. With no publisher, binary/oversize skill bundles refuse explicitly with
`UnsupportedCapability("skill_bundle_gcs_deployment")`; ordinary text bundles
retain their existing inline representation. External bundles still require a
UTF-8 root SKILL.md, safe canonical paths and no file/directory collisions.

The host must supply an authorized, idempotent publisher, immutable retention
and provider-readable GCS access; the driver discovers no bucket/credentials
and does not make content public. Its receipt is the publisher's attestation,
not an independent remote download verification. Offline tests carry all eleven
real default bundles byte-for-byte through a scripted publisher and the actual
SDK over MockTransport. They certify compiler/pin behavior, not a live GCS
deployment. F1's adapter supplies no publisher, so its dependency remains
honest. A text-only subset never establishes full default parity.

The Gemini API supports remote MCP over streamable HTTP, with optional headers
(`source-G-runtime.txt:587-613`), and filesystem-native skills under
`.agents/skills/` (`source-G-runtime.txt:751-752`). The driver's public MCP mapping
works, but authenticated daimon-mcp requires a credential resolver the driver
does not provide. The shared F1 session also needs the driver's required explicit
binding/thread extension. All six logical builtins route through native
`code_execution` (`source-G-runtime.txt:231,242-243`); native filesystem tool
identities are not modelled by the driver. The remaining gaps concern the
adapters for authorized skill publication, authenticated MCP and session binding.

No upstream deployment evidence or successful default turn is fabricated.
Removing the pending declaration cannot pass the current full scenario.
A component test scripts the real driver with native code-execution calls and
results, records normalized events and replays all six builtin routes twice.
Dropping any builtin call fails the turn check. This component supplies no
whole-default deployment evidence; full F1 stays typed PENDING. Incomplete replay
tapes fail. F1 is separate from C01–C18; the existing Gemini
matrix remains eight PASS and ten typed PENDING. No full live certificate exists.

Future live F2 requires an explicit lead GO, the pinned key file and reviewed
budget rates. The primary is `gemini-3.8-flash`, with fallback only after a 503 to
`gemini-flash-latest`, then `gemini-3.5-flash-lite`; key appearance alone does not
authorize a call. This adapter ships no live path.

F2 offline proof (`tests/parity/test_f2_gemini_default_capability.py`) explicitly
injects a full-byte GCS publisher and N3 credential resolver. All eleven authored
skills, including binary font assets, are retained and pinned; no asset filtering
or implicit publisher is enabled. The wire request maps the six logical builtins
to one Python code-execution tool and the neutral `daimon-mcp` toolset selector
to its declared native server once. Unbound selectors and confirmation policies
are refused. The scenario admits only two read-only MCP tools through N2’s real
loopback host; the provider endpoint and GCS/model execution remain replayed.
Valid authorization yields both host tool results; wrong/revoked bearers fail.
The normalized tape uses the merged credential-audited identity projection.
This is an offline SDK/host proof, with no native-agent CAS or live certification
claim. The uninjected adapter stays typed PENDING.

The bundle and F2 replay tests use `mux.testing.isolated_sdk.build_gemini_client`
with explicit synthetic keys, fixed routes and owned sync/async MockTransport
clients. Ambient provider names, including empty and unknown future settings,
refuse before SDK construction; proxy/TLS settings do not configure the clients.
Both clients close even when the proof fails. This test factory does not change
product client construction or the unconfigured backend.
